"""Price reaction tracking: price at event time and +15m / +1h / +1d."""

from __future__ import annotations

import logging
from datetime import timedelta

from . import db, market
from .util import iso, parse_dt, utcnow, utcnow_iso

log = logging.getLogger(__name__)

MACRO_SYMBOLS = ["SPY", "QQQ", "IWM", "^VIX", "^TNX", "DX-Y.NYB"]
MIN_IMPORTANCE = 3


def init_for_new_events() -> int:
    """Create reaction rows for recently analyzed, important events."""
    rows = db.query(
        """SELECT e.* FROM events e
           WHERE e.status = 'analyzed' AND e.importance >= ? AND e.published_at >= ?
             AND NOT EXISTS (SELECT 1 FROM price_reactions p WHERE p.event_id = e.id)""",
        (MIN_IMPORTANCE, iso(utcnow() - timedelta(days=2))),
    )
    n = 0
    for e in rows:
        if e["type"] in ("macro", "data"):
            syms = MACRO_SYMBOLS
        else:
            active = {t["symbol"] for t in db.active_tickers()}
            syms = [s for s in db.event_symbols(e["id"]) if s in active][:4] or ([e["symbol"]] if e["symbol"] else [])
        for s in syms:
            db.execute("INSERT OR IGNORE INTO price_reactions(event_id, symbol) VALUES (?, ?)", (e["id"], s))
            n += 1
    if n:
        update(only_missing_t0=True)
    return n


def update(only_missing_t0: bool = False) -> int:
    sql = """SELECT p.*, e.published_at FROM price_reactions p JOIN events e ON e.id = p.event_id
             WHERE p.done = 0"""
    if only_missing_t0:
        sql += " AND p.price_t0 IS NULL"
    rows = db.query(sql)
    n = 0
    for r in rows:
        t0 = parse_dt(r["published_at"])
        try:
            res = market.reaction(r["symbol"], t0)
        except Exception as exc:  # noqa: BLE001
            log.warning("reaction %s/%s failed: %s", r["event_id"], r["symbol"], exc)
            continue
        done = res["chg_1d"] is not None or utcnow() - t0 > timedelta(days=4)
        db.execute(
            """UPDATE price_reactions SET price_t0 = COALESCE(?, price_t0), chg_15m = ?, chg_1h = ?, chg_1d = ?,
                      done = ?, updated_at = ? WHERE event_id = ? AND symbol = ?""",
            (res["price_t0"], res["chg_15m"], res["chg_1h"], res["chg_1d"], int(done), utcnow_iso(), r["event_id"], r["symbol"]),
        )
        n += 1
    return n
