"""Yahoo Finance access (yfinance), serialized and lightly cached."""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import yfinance as yf

log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _lock:
        value = fn()
    _cache[key] = (now, value)
    return value


def _num(x) -> float | None:
    try:
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def lookup(symbol: str) -> dict | None:
    """Validate a ticker and return basic info, or None if Yahoo doesn't know it."""
    symbol = symbol.upper().strip()

    def fn():
        t = yf.Ticker(symbol)
        price = _num(getattr(t.fast_info, "last_price", None))
        if price is None:
            return None
        info = {}
        try:
            info = t.get_info() or {}
        except Exception as exc:  # noqa: BLE001 - Yahoo flakiness
            log.warning("info for %s failed: %s", symbol, exc)
        return {
            "symbol": symbol,
            "price": price,
            "name": info.get("longName") or info.get("shortName") or symbol,
            "short_name": info.get("shortName") or symbol,
            "sector": info.get("sector") or info.get("quoteType"),
        }

    try:
        return _cached(("lookup", symbol), 3600, fn)
    except Exception as exc:  # noqa: BLE001
        log.warning("lookup %s failed: %s", symbol, exc)
        return None


def quotes(symbols: list[str]) -> dict[str, dict]:
    """Last price + day change for symbols (cached 60s)."""
    out: dict[str, dict] = {}
    for s in symbols:
        def fn(s=s):
            fi = yf.Ticker(s).fast_info
            last = _num(fi.last_price)
            prev = _num(fi.previous_close)
            return {
                "price": last,
                "prev_close": prev,
                "change_pct": ((last / prev - 1) * 100) if last and prev else None,
            }
        try:
            out[s] = _cached(("quote", s), 60, fn)
        except Exception as exc:  # noqa: BLE001
            log.warning("quote %s failed: %s", s, exc)
            out[s] = {"price": None, "prev_close": None, "change_pct": None}
    return out


def intraday(symbol: str, days: int = 5):
    """5-minute bars incl. pre/post market (cached 4 min). Index is tz-aware."""
    def fn():
        return yf.Ticker(symbol).history(period=f"{days}d", interval="5m", prepost=True)
    return _cached(("intraday", symbol, days), 240, fn)


def daily(symbol: str, period: str = "6mo"):
    def fn():
        return yf.Ticker(symbol).history(period=period, interval="1d")
    return _cached(("daily", symbol, period), 900, fn)


def chart_series(symbol: str, range_: str) -> list[dict]:
    """Candles for the UI chart."""
    interval = {"1d": "5m", "5d": "15m", "1mo": "1h", "3mo": "1d", "6mo": "1d", "1y": "1d", "2y": "1wk"}.get(range_, "1d")

    def fn():
        return yf.Ticker(symbol).history(period=range_, interval=interval, prepost=interval in ("5m", "15m"))

    df = _cached(("chart", symbol, range_), 300, fn)
    out = []
    for ts, row in df.iterrows():
        o, h, l, c = (_num(row.get(k)) for k in ("Open", "High", "Low", "Close"))
        if None in (o, h, l, c):
            continue
        out.append({"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c})
    return out


def upgrades_downgrades(symbol: str):
    def fn():
        return yf.Ticker(symbol).upgrades_downgrades
    return _cached(("ud", symbol), 600, fn)


def price_targets(symbol: str) -> dict:
    def fn():
        return yf.Ticker(symbol).analyst_price_targets or {}
    try:
        return _cached(("pt", symbol), 3600, fn)
    except Exception:  # noqa: BLE001
        return {}


def yahoo_news(symbol: str) -> list[dict]:
    def fn():
        return yf.Ticker(symbol).news or []
    try:
        return _cached(("news", symbol), 120, fn)
    except Exception:  # noqa: BLE001
        return []


def price_at(symbol: str, when: datetime, *, after: bool) -> tuple[datetime, float] | None:
    """Price from 5m bars: last bar at/before `when` (after=False) or first bar at/after (after=True)."""
    try:
        df = intraday(symbol, 7)
    except Exception as exc:  # noqa: BLE001
        log.warning("intraday %s failed: %s", symbol, exc)
        return None
    if df is None or df.empty:
        return None
    when = when.astimezone(timezone.utc)
    idx = df.index.tz_convert("UTC")
    if after:
        mask = idx >= when
        if not mask.any():
            return None
        i = mask.argmax()
    else:
        mask = idx <= when
        if not mask.any():
            return None
        i = len(mask) - 1 - mask[::-1].argmax()
    price = _num(df["Close"].iloc[i])
    return (idx[i].to_pydatetime(), price) if price else None


def latest_bar_time(symbol: str) -> datetime | None:
    try:
        df = intraday(symbol, 7)
        return df.index.tz_convert("UTC")[-1].to_pydatetime() if not df.empty else None
    except Exception:  # noqa: BLE001
        return None


def reaction(symbol: str, t0: datetime) -> dict:
    """Price at event time and % changes at +15m / +1h / +1d (None if not yet available)."""
    base = price_at(symbol, t0, after=False) or price_at(symbol, t0, after=True)
    if not base:
        return {"price_t0": None, "chg_15m": None, "chg_1h": None, "chg_1d": None}
    _, p0 = base
    last = latest_bar_time(symbol)
    out = {"price_t0": p0}
    for key, delta in (("chg_15m", timedelta(minutes=15)), ("chg_1h", timedelta(hours=1)), ("chg_1d", timedelta(days=1))):
        target = t0 + delta
        hit = price_at(symbol, target, after=True) if last and last >= target else None
        out[key] = round((hit[1] / p0 - 1) * 100, 2) if hit else None
    return out
