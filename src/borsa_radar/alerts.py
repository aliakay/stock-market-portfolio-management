"""Price / level alerts ("NVDA 150 altına kapanırsa haber ver").

Conditions:
  below / above            — live quote crosses the level (checked every 15 min in US hours)
  close_below / close_above — daily close (checked after the closing price job)
  move                     — absolute day move ≥ level % (live)
One-shot by default; repeat=1 fires at most once per NY trading day.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from . import config, db, market, prices
from .util import tr_n, utcnow_iso

log = logging.getLogger(__name__)

COND_TR = {"below": "altına inerse", "above": "üstüne çıkarsa", "close_below": "altında kapanırsa",
           "close_above": "üstünde kapanırsa", "move": "günlük hareketi %{l} aşarsa"}
LIVE = ("below", "above", "move")


def label(a: dict) -> str:
    if a["cond"] == "move":
        return f"{a['symbol']} günlük hareketi %{tr_n(a['level'], 1).removesuffix(',0')} aşarsa"
    return f"{a['symbol']} {tr_n(a['level'], 2).removesuffix(',00')}$ {COND_TR[a['cond']]}"


def parse(text: str) -> dict:
    """'NVDA 150 altı', 'NVDA kapanış 150 altı', 'NVDA > 200', 'NVDA %5 hareket', 'NVDA close_below 150'."""
    t = text.strip().replace(",", ".")
    m = re.match(r"^\$?([A-Za-z.\-^=]+)\s+(.*)$", t)
    if not m:
        raise ValueError("biçim: SEMBOL SEVİYE altı|üstü  ·  SEMBOL kapanış SEVİYE altı  ·  SEMBOL %5 hareket")
    sym, rest = m.group(1).upper(), m.group(2).lower()
    close = "kapan" in rest or "close" in rest
    num = re.search(r"(\d+(?:\.\d+)?)", rest)
    if not num:
        raise ValueError("seviye bulunamadı")
    lvl = float(num.group(1))
    if "hareket" in rest or "move" in rest or "%" in rest and not any(w in rest for w in ("alt", "üst", "<", ">")):
        return {"symbol": sym, "cond": "move", "level": lvl}
    if any(w in rest for w in ("alt", "below", "<", "düş")):
        side = "below"
    elif any(w in rest for w in ("üst", "above", ">", "çık", "geç")):
        side = "above"
    else:
        raise ValueError("yön belirsiz: 'altı' ya da 'üstü' yaz")
    return {"symbol": sym, "cond": ("close_" if close else "") + side, "level": lvl}


def add(symbol: str, cond: str, level: float, *, note: str | None = None, repeat: bool = False) -> int:
    if cond not in COND_TR:
        raise ValueError(f"bilinmeyen koşul: {cond}")
    return db.execute("INSERT INTO price_alerts(symbol, cond, level, note, repeat, created_at) VALUES (?,?,?,?,?,?)",
                      (symbol.upper(), cond, float(level), note, 1 if repeat else 0, utcnow_iso()))


def remove(aid: int) -> None:
    db.execute("DELETE FROM price_alerts WHERE id = ?", (aid,))


def list_(symbol: str | None = None, include_inactive: bool = False) -> list[dict]:
    where, params = [], []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol.upper())
    if not include_inactive:
        where.append("active = 1")
    rows = db.query("SELECT * FROM price_alerts" + (" WHERE " + " AND ".join(where) if where else "") +
                    " ORDER BY active DESC, symbol, level", params)
    return [dict(r) | {"label": label(dict(r))} for r in rows]


def _ny_day() -> str:
    return datetime.now(config.NY_TZ).date().isoformat()


def _fire(a: dict, value: float, when: str) -> None:
    if a["repeat"] and a["fired_at"] and a["fired_at"][:10] == _ny_day():
        return
    db.execute("UPDATE price_alerts SET fired_at = ?, fired_value = ?, fired_count = fired_count + 1, active = ? WHERE id = ?",
               (utcnow_iso(), value, 1 if a["repeat"] else 0, a["id"]))
    if db.get_setting("price_alerts") == "0":
        return
    from . import notifier
    v = f"%{tr_n(value, 1, True)}" if a["cond"] == "move" else f"{tr_n(value, 2)}$"
    icon = "🔔"
    notifier.enqueue(f"{icon} <b>Fiyat alarmı · {a['symbol']}</b>\n{label(a)} → şu an {v} ({when})"
                     + (f"\nNot: {a['note']}" if a["note"] else "") + f"\n🔗 {config.PUBLIC_BASE_URL}/h/{a['symbol']}", kind="price_alert")


def check_live() -> int:
    rows = [a for a in list_() if a["cond"] in LIVE]
    if not rows:
        return 0
    q = market.quotes(sorted({a["symbol"] for a in rows}))
    n = 0
    for a in rows:
        v = q.get(a["symbol"]) or {}
        px, ch = v.get("price"), v.get("change_pct")
        if a["cond"] == "below" and px is not None and px < a["level"]:
            _fire(a, px, "anlık"); n += 1
        elif a["cond"] == "above" and px is not None and px > a["level"]:
            _fire(a, px, "anlık"); n += 1
        elif a["cond"] == "move" and ch is not None and abs(ch) >= a["level"]:
            _fire(a, ch, "anlık"); n += 1
    return n


def check_close() -> int:
    rows = [a for a in list_() if a["cond"] in ("close_below", "close_above")]
    n = 0
    for a in rows:
        last = prices.series(a["symbol"], a["created_at"][:10]).last()
        if not last or last[0] < a["created_at"][:10]:
            continue
        d, px = last
        if a["repeat"] and a["fired_at"] and a["fired_at"][:10] >= d:
            continue
        if (a["cond"] == "close_below" and px < a["level"]) or (a["cond"] == "close_above" and px > a["level"]):
            _fire(a, px, f"{d} kapanışı"); n += 1
    return n
