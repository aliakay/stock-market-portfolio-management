"""Daily bar store shared by every engine (portfolio, regime, rotation, setups).

Source: Yahoo via yfinance batch download. `close` is split-adjusted but not
dividend-adjusted (Yahoo "Close"); `adj_close` is the dividend-adjusted series.
Split events are stored separately so ledger quantities can be put on the same
share basis as the closes.
"""

from __future__ import annotations

import logging
import math
import threading
from bisect import bisect_right
from datetime import date, timedelta
from typing import Iterable

import yfinance as yf

from . import config, db
from .util import utcnow_iso

log = logging.getLogger(__name__)
SOURCE = "yahoo"
DEFAULT_HISTORY_DAYS = 3 * 366
_lock = threading.Lock()


def _num(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _frame_for(df, symbol: str, many: bool):
    """Slice one ticker out of a yf.download frame (MultiIndex or flat)."""
    if df is None or df.empty:
        return None
    cols = df.columns
    if getattr(cols, "nlevels", 1) > 1:
        lv0 = set(cols.get_level_values(0))
        if symbol in lv0:
            return df[symbol]
        lv1 = set(cols.get_level_values(1))
        if symbol in lv1:
            return df.xs(symbol, axis=1, level=1)
        return None
    return None if many else df


def _download(symbols: list[str], start: date):
    return yf.download(
        symbols, start=start.isoformat(), interval="1d", auto_adjust=False, actions=True,
        group_by="ticker", progress=False, threads=True, multi_level_index=True,
    )


def refresh(symbols: Iterable[str], *, start: date | None = None, full: bool = False) -> dict[str, int]:
    """Fetch/extend daily bars. Incremental per symbol (re-reads the last week to
    pick up revisions and today's partial bar). Returns {symbol: rows written}."""
    syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not syms:
        return {}
    last = {r["symbol"]: r["d"] for r in db.query(
        f"SELECT symbol, MAX(day) d FROM prices_daily WHERE symbol IN ({','.join('?' * len(syms))}) GROUP BY symbol", syms)}
    default_start = start or (date.today() - timedelta(days=DEFAULT_HISTORY_DAYS))
    buckets: dict[date, list[str]] = {}
    for s in syms:
        if full or s not in last:
            st = default_start
        else:
            st = min(date.fromisoformat(last[s]) - timedelta(days=7), start or date.max)
        buckets.setdefault(st, []).append(s)

    written: dict[str, int] = {}
    now = utcnow_iso()
    with _lock:
        for st, group in sorted(buckets.items()):
            for i in range(0, len(group), 40):
                chunk = group[i:i + 40]
                try:
                    df = _download(chunk, st)
                except Exception as exc:  # noqa: BLE001 - Yahoo flakiness
                    log.warning("price download %s failed: %s", chunk[:3], exc)
                    continue
                for s in chunk:
                    f = _frame_for(df, s, len(chunk) > 1)
                    if f is None or f.empty:
                        written[s] = 0
                        continue
                    rows, split_rows = [], []
                    for ts, r in f.iterrows():
                        close = _num(r.get("Close"))
                        if close is None:
                            continue
                        day = ts.date().isoformat()
                        rows.append((s, day, _num(r.get("Open")), _num(r.get("High")), _num(r.get("Low")), close,
                                     _num(r.get("Adj Close")), _num(r.get("Volume")), SOURCE, now))
                        sp = _num(r.get("Stock Splits"))
                        if sp and sp > 0 and sp != 1:
                            split_rows.append((s, day, sp))
                    with db.tx() as c:
                        c.executemany(
                            """INSERT INTO prices_daily(symbol, day, open, high, low, close, adj_close, volume, source, updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(symbol, day) DO UPDATE SET open=excluded.open, high=excluded.high,
                                 low=excluded.low, close=excluded.close, adj_close=excluded.adj_close,
                                 volume=excluded.volume, source=excluded.source, updated_at=excluded.updated_at""",
                            rows,
                        )
                        c.executemany("INSERT OR REPLACE INTO splits(symbol, day, ratio) VALUES (?,?,?)", split_rows)
                    written[s] = len(rows)
    return written


# ── reads ─────────────────────────────────────────────────

class Series:
    """Close series with as-of (forward-filled) lookup."""

    __slots__ = ("symbol", "days", "closes")

    def __init__(self, symbol: str, rows: list[tuple[str, float]]):
        self.symbol = symbol
        self.days = [d for d, _ in rows]
        self.closes = [c for _, c in rows]

    def __bool__(self) -> bool:
        return bool(self.days)

    def at(self, day: str) -> float | None:
        """Last close on or before `day`."""
        i = bisect_right(self.days, day) - 1
        return self.closes[i] if i >= 0 else None

    def last(self) -> tuple[str, float] | None:
        return (self.days[-1], self.closes[-1]) if self.days else None


def series(symbol: str, start: str | None = None) -> Series:
    rows = db.query(
        "SELECT day, close FROM prices_daily WHERE symbol = ? AND day >= ? AND close IS NOT NULL ORDER BY day",
        (symbol.upper(), start or "0000"),
    )
    return Series(symbol.upper(), [(r["day"], r["close"]) for r in rows])


def series_many(symbols: Iterable[str], start: str | None = None) -> dict[str, Series]:
    return {s: series(s, start) for s in {x.upper() for x in symbols}}


def bars(symbol: str, start: str | None = None) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT day, open, high, low, close, adj_close, volume FROM prices_daily WHERE symbol = ? AND day >= ? ORDER BY day",
        (symbol.upper(), start or "0000"))]


def trading_days(start: str, end: str | None = None) -> list[str]:
    rows = db.query(
        "SELECT day FROM prices_daily WHERE symbol = ? AND day >= ? AND day <= ? ORDER BY day",
        (config.CALENDAR_SYMBOL, start, end or "9999"),
    )
    return [r["day"] for r in rows]


def split_factors(symbol: str) -> list[tuple[str, float]]:
    return [(r["day"], r["ratio"]) for r in db.query(
        "SELECT day, ratio FROM splits WHERE symbol = ? ORDER BY day", (symbol.upper(),))]


def split_factor_after(symbol: str, day: str, cache: dict | None = None) -> float:
    """Product of split ratios with ex-date after `day` (qty × factor = today's share basis)."""
    sp = cache.setdefault(symbol, split_factors(symbol)) if cache is not None else split_factors(symbol)
    f = 1.0
    for d, ratio in sp:
        if d > day:
            f *= ratio
    return f


def last_update(symbol: str = config.CALENDAR_SYMBOL) -> dict | None:
    r = db.query_one("SELECT day, updated_at FROM prices_daily WHERE symbol = ? ORDER BY day DESC LIMIT 1", (symbol,))
    return dict(r) if r else None


def fx_symbol(currency: str) -> str | None:
    cur = (currency or "USD").upper()
    return None if cur == "USD" else f"{cur}USD=X"
