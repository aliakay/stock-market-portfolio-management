"""Kalshi prediction markets: Fed decision odds, rate-cut count, CPI / jobs / unemployment / GDP / recession.

Probabilities = bid/ask midpoint (fallback last price). Big moves (>= 10 pts in ~24h) become macro events.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import httpx

from .. import db
from ..util import iso, sha, utcnow

log = logging.getLogger(__name__)
BASE = "https://api.elections.kalshi.com/trade-api/v2"

# series -> (Turkish label, how many upcoming events to keep, kind)
SERIES = {
    "KXFEDDECISION": ("Fed kararı", 2, "categorical"),
    "KXRATECUTCOUNT": ("Yıl içi faiz indirimi sayısı", 1, "categorical"),
    "KXCPIYOY": ("CPI yıllık", 1, "ladder"),
    "KXCPI": ("CPI aylık", 1, "ladder"),
    "KXPAYROLLS": ("Tarım dışı istihdam", 1, "ladder"),
    "KXU3": ("İşsizlik oranı", 1, "ladder"),
    "KXGDP": ("GSYİH büyümesi", 1, "ladder"),
    "KXRECSSNBER": ("Resesyon", 1, "categorical"),
}
SHIFT_PTS = 0.10
SHIFT_SERIES = {"KXFEDDECISION", "KXRECSSNBER", "KXRATECUTCOUNT"}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _prob(m: dict) -> float | None:
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    last = _f(m.get("last_price_dollars"))
    if bid is not None and ask is not None and 0 < ask - bid <= 0.15:
        return round((bid + ask) / 2, 4)
    return last if last is not None else bid


def collect() -> int:
    now = iso(utcnow())
    n = 0
    for series, (_, keep, _) in SERIES.items():
        try:
            r = httpx.get(f"{BASE}/events", timeout=30, params={
                "series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 10})
            r.raise_for_status()
            events = sorted(r.json().get("events") or [], key=lambda e: min(
                (m.get("close_time") or "9") for m in (e.get("markets") or [{}])))[:keep]
        except Exception as exc:  # noqa: BLE001
            log.warning("kalshi %s failed: %s", series, exc)
            continue
        for e in events:
            for m in e.get("markets") or []:
                p = _prob(m)
                label = m.get("yes_sub_title") or m.get("subtitle") or m["ticker"]
                db.execute(
                    """INSERT INTO kalshi_markets(market_ticker, event_ticker, series, event_title, label, prob, close_time, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(market_ticker) DO UPDATE SET prob = excluded.prob, label = excluded.label,
                         event_title = excluded.event_title, close_time = excluded.close_time, updated_at = excluded.updated_at""",
                    (m["ticker"], e["event_ticker"], series, e.get("title"), label, p, m.get("close_time"), now),
                )
                if p is not None:
                    db.execute("INSERT OR IGNORE INTO kalshi_history(market_ticker, ts, prob) VALUES (?,?,?)", (m["ticker"], now, p))
                n += 1
    # drop markets of closed events
    db.execute("DELETE FROM kalshi_markets WHERE updated_at < ?", (iso(utcnow() - timedelta(days=2)),))
    db.execute("DELETE FROM kalshi_history WHERE ts < ?", (iso(utcnow() - timedelta(days=60)),))
    _detect_shifts()
    return n


def _detect_shifts() -> None:
    """Create a macro event when a headline probability moves >= 10 pts vs ~24h ago."""
    first_run = db.get_cursor("kalshi_seen") is None
    db.set_cursor("kalshi_seen", "1")
    if first_run:
        return
    ago = iso(utcnow() - timedelta(hours=24))
    for m in db.query(f"SELECT * FROM kalshi_markets WHERE series IN ({','.join('?' * len(SHIFT_SERIES))}) AND prob IS NOT NULL",
                      tuple(SHIFT_SERIES)):
        old = db.query_one("SELECT prob FROM kalshi_history WHERE market_ticker = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                           (m["market_ticker"], ago))
        if not old or old["prob"] is None:
            continue
        delta = m["prob"] - old["prob"]
        if abs(delta) < SHIFT_PTS:
            continue
        day = utcnow().strftime("%Y-%m-%d")
        title = (f"🎲 Kalshi: \"{m['event_title']} — {m['label']}\" olasılığı %{old['prob']*100:.0f} → %{m['prob']*100:.0f} "
                 f"({delta*100:+.0f} puan, 24 saat)")
        db.insert_event(
            type="macro", source="Kalshi", title=title, url=f"https://kalshi.com/markets/{m['series'].lower()}",
            body=f"Tahmin piyasası olasılık değişimi. Güncel dağılım: {summary_text(m['event_ticker'])}",
            published_at=iso(utcnow()), dedupe_key=sha("kalshi-shift", m["market_ticker"], day),
            status="pending", extra={"market": m["market_ticker"], "prob": m["prob"], "prob_24h": old["prob"]},
        )


def implied_median(event_ticker: str) -> tuple[str | None, float | None]:
    """For 'Above X' ladders: threshold where P(above) crosses 50%."""
    rows = db.query("SELECT label, prob FROM kalshi_markets WHERE event_ticker = ? AND prob IS NOT NULL", (event_ticker,))
    pts = []
    for r in rows:
        lab = (r["label"] or "").replace(",", "")
        m = re.search(r"(-?\d+(?:\.\d+)?)", lab)
        if lab.lower().startswith("above") and m:
            pts.append((float(m.group(1)), r["prob"]))
    pts.sort()
    for (x1, p1), (x2, p2) in zip(pts, pts[1:]):
        if p1 >= 0.5 >= p2:
            frac = 0 if p1 == p2 else (p1 - 0.5) / (p1 - p2)
            return (rows[0]["label"] if rows else None), round(x1 + frac * (x2 - x1), 3)
    return None, None


def summary_text(event_ticker: str, top: int = 6) -> str:
    rows = db.query("SELECT label, prob FROM kalshi_markets WHERE event_ticker = ? AND prob IS NOT NULL ORDER BY prob DESC", (event_ticker,))
    return ", ".join(f"{r['label']}: %{r['prob']*100:.0f}" for r in rows[:top])


def overview() -> list[dict]:
    """Grouped data for the UI / digest / research context."""
    out = []
    for series, (label, _, kind) in SERIES.items():
        evs = db.query("SELECT DISTINCT event_ticker, event_title, MIN(close_time) ct FROM kalshi_markets WHERE series = ? GROUP BY event_ticker ORDER BY ct",
                       (series,))
        for e in evs:
            rows = db.query("SELECT * FROM kalshi_markets WHERE event_ticker = ? AND prob IS NOT NULL", (e["event_ticker"],))
            if kind == "categorical":
                items = sorted(rows, key=lambda r: -r["prob"])
            else:
                items = sorted(rows, key=lambda r: r["label"])
            ago = iso(utcnow() - timedelta(hours=24))
            enriched = []
            for r in items:
                old = db.query_one("SELECT prob FROM kalshi_history WHERE market_ticker = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                                   (r["market_ticker"], ago))
                enriched.append({"label": r["label"], "prob": r["prob"],
                                 "delta": (r["prob"] - old["prob"]) if old and old["prob"] is not None else None})
            med = implied_median(e["event_ticker"])[1] if kind == "ladder" else None
            out.append({"series": series, "label": label, "kind": kind, "event_ticker": e["event_ticker"],
                        "title": e["event_title"], "close": e["ct"], "items": enriched, "median": med})
    return out


def context_text() -> str:
    """Compact text for LLM prompts."""
    lines = []
    for g in overview():
        if g["kind"] == "categorical":
            top = ", ".join(f"{i['label']} %{i['prob']*100:.0f}" for i in g["items"][:4])
            lines.append(f"- {g['label']} ({g['title']}): {top}")
        elif g["median"] is not None:
            lines.append(f"- {g['label']} ({g['title']}): piyasa medyan beklentisi ≈ {g['median']}")
    return "\n".join(lines)
