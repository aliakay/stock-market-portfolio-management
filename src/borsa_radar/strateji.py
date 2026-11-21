"""Strateji laboratuvarı, live side.

The lab (`lab.py`) judges mechanical swing strategies on ~20 years of bars. The ones that pass are listed in
STRATEGIES. Every night after the setup scan this module:

1. follows every open paper trade on real bars (fill at the next open or a one-session limit, exit at the open after
   the exit rule fires, time exit, optional stop) — the exact functions the backtest uses;
2. scans the own list + discovery universe for today's signals and opens paper trades for them; a capped book of
   BOOK_SLOTS positions takes the most stretched ones first, like the backtest portfolio replay.

Nothing here places orders. The page shows today's signals as trade ideas with the rule that would end them, and the
paper record next to the backtest record so the two can be compared honestly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from . import config, db, lab, prices
from .util import utcnow_iso

log = logging.getLogger(__name__)
RESULTS_KEY = "lab_results"
BOOK_SLOTS = 10
HISTORY_DAYS = 420            # calendar days of bars per symbol for the live scan (SMA200 + 100-day rank)


@dataclass(frozen=True)
class Strategy:
    code: str
    name: str
    spec: lab.Spec
    what: str          # one plain sentence: the idea
    entry_tr: str      # how to enter
    exit_tr: str       # how to exit


def _strategy(code: str, name: str, spec: lab.Spec, what: str) -> Strategy:
    entry, exit_ = lab.describe(spec)
    return Strategy(code, name, spec, what, entry, exit_)


# Chosen on 2006-2016 only (train win ≥ 80 %, ≥ 150 trades, profit factor ≥ 1.5; among those, the market filter halved
# the worst trade and a 30-session limit raised the win rate in every family), then judged once on 2017 → today.
# Stage results: borsa-radar lab run (stored record) · research notes in the README "Strateji laboratuvarı" section.
STRATEGIES: list[Strategy] = [
    _strategy("derin", "Derin düşüş alımı",
              lab.Spec("derin", "crsi", 15, "above_entry", mode="limit", limit=0.06, hold=30, macro="all"),
              "Uzun vadede yükselen, likit bir hisse birkaç günde sert satıldığında, panik satışın içine kapanışın %6 altından limit "
              "emirle girer; fiyat alış maliyetinin üstünde kapattığı ilk günün ertesi sabahı çıkar. Piyasanın geneli bozukken "
              "(S&P 500 200 günlük ortalamanın altında, VIX 30 üstü ya da kredi piyasası gerginken) sinyal üretmez."),
    _strategy("geri", "Geri çekilme alımı",
              lab.Spec("geri", "crsi", 15, "above_entry", mode="limit", limit=0.04, hold=30, macro="all"),
              "Derin düşüş alımıyla aynı kural, limit emir kapanışın %4 altında: daha sık dolar (yaklaşık üç kat işlem), "
              "başarı oranı biraz daha düşük. İki strateji aynı fırsatlara bakar; bir hisse ikisinde birden sinyal verebilir."),
]


def by_code(code: str) -> Strategy | None:
    return next((s for s in STRATEGIES if s.code == code), None)


def _d(i: int) -> str:
    return f"{i // 10000:04d}-{i // 100 % 100:02d}-{i % 100:02d}"


def _i(d: str) -> int:
    return int(d.replace("-", ""))


def _ind(sym: str, until: str | None = None) -> lab.Ind | None:
    start = (date.today() - timedelta(days=HISTORY_DAYS + 30)).isoformat()
    rows = [b for b in prices.bars(sym, start) if b["close"] is not None and (until is None or b["day"] <= until)]
    if len(rows) < 230:
        return None
    arr = np.array([[_i(b["day"]), b["open"] or b["close"], b["high"] or b["close"], b["low"] or b["close"], b["close"],
                     b["volume"] or 0] for b in rows], dtype=np.float64)
    return lab.Ind(arr)


def live_macro() -> lab.Macro | None:
    """Market filters from the live store. Dividend-adjusted closes, as in the lab: HYG pays a fat monthly dividend,
    so a raw HYG/LQD ratio drifts down at every ex-date and would read as credit stress."""
    def get(sym):
        rows = [b for b in prices.bars(sym) if b["close"] is not None]
        if not rows:
            return None
        return np.array([[_i(b["day"]), b["open"] or b["close"], b["high"] or b["close"], b["low"] or b["close"],
                          b["adj_close"] or b["close"], b["volume"] or 0] for b in rows], dtype=np.float64)
    try:
        return lab.build_macro(get)
    except Exception:  # noqa: BLE001
        log.exception("live macro failed")
        return None


# ── follow open paper trades ──────────────────────────────

def follow(row: dict, x: lab.Ind) -> dict:
    """PURE: new state of one paper trade from its symbol's bars."""
    st = by_code(row["strategy"])
    if st is None:
        return {}
    days = list(x.day)
    sig = _i(row["sig_day"])
    if sig not in days:
        return {}
    t = days.index(sig)
    if t + 1 >= x.n:
        return {"status": "pending"}
    tr = lab.follow_one(st.spec, row["symbol"], x, t, lab.exit_mask(st.spec, x), row.get("rank") or 50.0)
    if tr is None:
        return {"status": "lapsed"}       # the limit was not reached in its one session
    last_px, last_day = float(x.c[-1]), _d(int(x.day[-1]))
    out = {"entry_day": _d(tr.entry_day), "entry_px": tr.entry, "last_px": last_px, "last_day": last_day}
    if tr.why in ("open", "exit_next"):
        out.update(status="open", exit_next=1 if tr.why == "exit_next" else 0, ret=(last_px / tr.entry - 1) * 100,
                   held=tr.hold + 1)
    else:
        out.update(status="closed", exit_day=_d(tr.exit_day), exit_px=tr.exit, ret=tr.ret * 100, why=tr.why, exit_next=0,
                   held=tr.hold)
    return out


def update_open() -> int:
    rows = [dict(r) for r in db.query("SELECT * FROM paper_trades WHERE status IN ('pending', 'open')")]
    cache: dict[str, lab.Ind | None] = {}
    n = 0
    for r in rows:
        if r["symbol"] not in cache:
            cache[r["symbol"]] = _ind(r["symbol"])
        x = cache[r["symbol"]]
        if x is None:
            continue
        new = follow(r, x)
        if not new or all(r.get(k) == v for k, v in new.items()):
            continue
        cols = ", ".join(f"{k} = ?" for k in new)
        db.execute(f"UPDATE paper_trades SET {cols}, updated_at = ? WHERE id = ?", (*new.values(), utcnow_iso(), r["id"]))
        n += 1
    return n


# ── today's signals ───────────────────────────────────────

def scan(day: str | None = None) -> dict:
    """Signals at the close of `day` (default: last SPY bar) for every strategy, stored as pending paper trades."""
    from . import setups
    if not STRATEGIES:
        return {"day": day, "signals": 0}
    day = day or setups.base_day()
    if not day:
        return {"day": None, "signals": 0}
    uni = setups.universe(wide=True)
    m = live_macro()
    found: dict[str, list[tuple[float, dict, float]]] = {s.code: [] for s in STRATEGIES}
    for u in uni:
        x = _ind(u["symbol"], until=day)
        if x is None or _d(int(x.day[-1])) != day:
            continue
        t = x.n - 1
        for st in STRATEGIES:
            ent = lab.entry_mask(st.spec, x) & lab.macro_mask(st.spec, x, m)
            if not ent[t]:
                continue
            rk = float(lab.rank_value(st.spec, x)[t])
            found[st.code].append((rk, u, float(x.c[t])))
    total = 0
    for st in STRATEGIES:
        busy = {r["symbol"] for r in db.query("SELECT symbol FROM paper_trades WHERE strategy = ? AND status IN ('pending','open')",
                                              (st.code,))}
        in_book = db.query_one("SELECT COUNT(*) n FROM paper_trades WHERE strategy = ? AND in_book = 1 AND status IN ('pending','open')",
                               (st.code,))["n"]
        free = BOOK_SLOTS - in_book
        for rk, u, close in sorted(found[st.code], key=lambda z: z[0]):
            if u["symbol"] in busy:
                continue                      # one position per symbol per strategy, as in the backtest
            take = 1 if free > 0 else 0
            free -= take
            lim = round(close * (1 - st.spec.limit), 2) if st.spec.mode == "limit" else None
            body = {"company": u.get("company"), "sector": u.get("sector"), "src": u.get("src"), "held": u.get("held"), "close": close}
            db.execute("""INSERT INTO paper_trades(strategy, symbol, sig_day, tier, rank, limit_px, status, in_book, body, created_at, updated_at)
                          VALUES (?,?,?,?,?,?, 'pending', ?,?,?,?)
                          ON CONFLICT(strategy, symbol, sig_day) DO UPDATE SET rank=excluded.rank, limit_px=excluded.limit_px,
                            body=excluded.body, updated_at=excluded.updated_at""",
                       (st.code, u["symbol"], day, u.get("tier") or "liste", rk, lim, take, json.dumps(body, ensure_ascii=False),
                        utcnow_iso(), utcnow_iso()))
            total += 1
    return {"day": day, "signals": total}


def daily_job() -> dict:
    """Nightly, after the setup scan (bars are fresh by then)."""
    n_upd = update_open()
    res = scan()
    db.set_cursor("strateji_last_run", json.dumps({"at": utcnow_iso(), "updated": n_upd, **res}))
    return {"updated": n_upd, **res}


# ── read models ───────────────────────────────────────────

def _load(r) -> dict:
    d = dict(r)
    d.update(json.loads(d.pop("body") or "{}"))
    st = by_code(d["strategy"])
    d["strategy_name"] = st.name if st else d["strategy"]
    return d


def signals(day: str | None = None) -> list[dict]:
    if day is None:
        r = db.query_one("SELECT MAX(sig_day) d FROM paper_trades")
        day = r["d"] if r else None
    if not day:
        return []
    return [_load(r) for r in db.query("SELECT * FROM paper_trades WHERE sig_day = ? ORDER BY in_book DESC, strategy, rank", (day,))]


def open_trades() -> list[dict]:
    return [_load(r) for r in db.query("SELECT * FROM paper_trades WHERE status = 'open' ORDER BY in_book DESC, entry_day DESC")]


def closed_trades(limit: int = 40) -> list[dict]:
    return [_load(r) for r in db.query("SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY exit_day DESC, id DESC LIMIT ?",
                                       (limit,))]


def paper_record() -> dict:
    """Live paper record per strategy: every signal, and the capped book only."""
    out = {}
    for st in STRATEGIES:
        rows = [dict(r) for r in db.query("SELECT ret, in_book FROM paper_trades WHERE strategy = ? AND status = 'closed'", (st.code,))]
        def summ(rs):
            if not rs:
                return {"n": 0}
            r = [x["ret"] for x in rs]
            w = [v for v in r if v > 0]
            l_ = [v for v in r if v <= 0]
            return {"n": len(r), "win": len(w) / len(r) * 100, "avg": sum(r) / len(r),
                    "avg_win": sum(w) / len(w) if w else None, "avg_loss": sum(l_) / len(l_) if l_ else None}
        out[st.code] = {"all": summ(rows), "book": summ([x for x in rows if x["in_book"]]),
                        "open": db.query_one("SELECT COUNT(*) n FROM paper_trades WHERE strategy = ? AND status = 'open'", (st.code,))["n"]}
    return out


def results() -> dict | None:
    """Backtest results saved by the last lab run (`borsa-radar lab run`)."""
    r = db.get_cursor(RESULTS_KEY)
    return json.loads(r) if r else None
