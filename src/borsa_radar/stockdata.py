"""Cached per-symbol data from FMP (stable API) and Yahoo (yfinance).

Every call returns a `Fetched` (data + fetch time + source) so the engines can
stamp each number with where and when it came from. Responses are cached in
`api_cache`; if a refresh fails the last good copy is returned marked `stale`.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import yfinance as yf

from . import config, db
from .util import iso, parse_dt, utcnow, utcnow_iso

log = logging.getLogger(__name__)
FMP_BASE = "https://financialmodelingprep.com/stable/"
_yf_lock = threading.Lock()
_key_lock = threading.Lock()
_inflight: dict[str, threading.Lock] = {}


@dataclass
class Fetched:
    data: object
    fetched_at: str | None
    source: str
    stale: bool = False
    error: str | None = None

    def __bool__(self) -> bool:
        return self.data not in (None, [], {})


def _lock_for(key: str) -> threading.Lock:
    with _key_lock:
        return _inflight.setdefault(key, threading.Lock())


def _cached(key: str, source: str, ttl: timedelta, fn) -> Fetched:
    with _lock_for(key):
        row = db.query_one("SELECT * FROM api_cache WHERE key = ?", (key,))
        if row and utcnow() - parse_dt(row["fetched_at"]) < ttl:
            return Fetched(json.loads(row["body"]), row["fetched_at"], source)
        try:
            data = fn()
        except Exception as exc:  # noqa: BLE001 - any upstream failure falls back to cache
            (log.debug if isinstance(exc, Blocked) else log.warning)("%s fetch failed: %s", key, exc)
            if row:
                return Fetched(json.loads(row["body"]), row["fetched_at"], source, stale=True, error=str(exc)[:200])
            return Fetched(None, None, source, error=str(exc)[:200])
        now = utcnow_iso()
        db.execute("INSERT INTO api_cache(key, source, fetched_at, body) VALUES (?,?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET source = excluded.source, fetched_at = excluded.fetched_at, body = excluded.body",
                   (key, source, now, json.dumps(data, default=str)))
        return Fetched(data, now, source)


# ── FMP ───────────────────────────────────────────────────

class Restricted(RuntimeError):
    pass


class Blocked(RuntimeError):
    """FMP answered 429 a short while ago: no call is made until the wait is over."""


FMP_BACKOFF_MIN = 30   # free plan: once the quota says no, every further call is wasted (and counted) until it resets
FMP_BLOCK_CURSOR = "fmp_blocked_until"


def fmp_blocked_until() -> str | None:
    """ISO time until which FMP calls are skipped (shared by the service and the CLI via the DB), or None."""
    until = db.get_cursor(FMP_BLOCK_CURSOR)
    return until if until and iso(utcnow()) < until else None


def _fmp_get(path: str, params: dict) -> object:
    key = config.secret("FMP_API_KEY")
    if not key:
        raise RuntimeError("FMP_API_KEY yok")
    if fmp_blocked_until():
        raise Blocked(f"{path}: FMP beklemede (HTTP 429 sonrası)")
    r = httpx.get(FMP_BASE + path, params=params | {"apikey": key}, timeout=25)
    if r.status_code == 429:
        db.set_cursor(FMP_BLOCK_CURSOR, iso(utcnow() + timedelta(minutes=FMP_BACKOFF_MIN)))
        raise RuntimeError(f"{path}: HTTP 429")
    if r.status_code == 402:  # not in the plan for this symbol: cache "nothing" instead of retrying every call
        log.info("%s %s: aboneliğe dahil değil", path, params.get("symbol"))
        return []
    if r.status_code >= 400:  # never let the URL (it carries the API key) reach logs or the UI
        raise RuntimeError(f"{path}: HTTP {r.status_code}")
    data = r.json()
    if isinstance(data, dict) and data.get("Error Message"):
        raise RuntimeError(data["Error Message"][:200])
    return data


def fmp(path: str, ttl_hours: float = 12, **params) -> Fetched:
    key = "fmp:" + path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return _cached(key, "FMP", timedelta(hours=ttl_hours), lambda: _fmp_get(path, params))


def fmp_one(path: str, ttl_hours: float = 12, **params) -> Fetched:
    f = fmp(path, ttl_hours, **params)
    if isinstance(f.data, list):
        f = Fetched(f.data[0] if f.data else None, f.fetched_at, f.source, f.stale, f.error)
    return f


# ── Yahoo ─────────────────────────────────────────────────

def _clean(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):
        try:
            return _clean(v.item())
        except (ValueError, AttributeError):
            pass
    return v


def _frame_records(df, index_name: str = "index") -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for idx, row in df.iterrows():
        rec = {index_name: _clean(idx)}
        rec.update({str(k): _clean(v) for k, v in row.items()})
        out.append(rec)
    return out


YAHOO_FRAMES = {
    "eps_trend": "period", "earnings_estimate": "period", "revenue_estimate": "period",
    "earnings_history": "quarter", "growth_estimates": "period", "insider_transactions": "i",
}


def yahoo(kind: str, symbol: str, ttl_hours: float = 6) -> Fetched:
    symbol = symbol.upper()

    def fn():
        with _yf_lock:
            t = yf.Ticker(symbol)
            if kind == "calendar":
                cal = t.calendar or {}
                return {k: ([_clean(x) for x in v] if isinstance(v, list) else _clean(v)) for k, v in cal.items()}
            if kind == "info":
                info = t.get_info() or {}
                keep = ("longName", "shortName", "sector", "industry", "marketCap", "beta", "trailingPE", "forwardPE",
                        "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "sharesOutstanding", "floatShares", "shortPercentOfFloat",
                        "heldPercentInsiders", "heldPercentInstitutions", "longBusinessSummary", "fullTimeEmployees",
                        "website", "currency", "quoteType", "averageVolume")
                return {k: _clean(info.get(k)) for k in keep}
            return _frame_records(getattr(t, kind), YAHOO_FRAMES.get(kind, "index"))
    return _cached(f"yahoo:{kind}:{symbol}", "Yahoo", timedelta(hours=ttl_hours), fn)


def option_chains(symbol: str, max_days: int = 60, max_expiries: int = 8, ttl_minutes: float = 30) -> Fetched:
    """Calls/puts per expiry for expiries within `max_days`, plus the spot used."""
    symbol = symbol.upper()

    def fn():
        with _yf_lock:
            t = yf.Ticker(symbol)
            exps = list(t.options or [])
            spot = t.fast_info.last_price
        today = datetime.now(config.NY_TZ).date()
        picked = [e for e in exps if 0 <= (datetime.fromisoformat(e).date() - today).days <= max_days][:max_expiries]
        out = []
        for e in picked:
            with _yf_lock:
                ch = t.option_chain(e)
            def rows(df):
                return [{"strike": _clean(r["strike"]), "oi": _clean(r.get("openInterest")) or 0, "vol": _clean(r.get("volume")) or 0,
                         "iv": _clean(r.get("impliedVolatility")), "last": _clean(r.get("lastPrice")),
                         "bid": _clean(r.get("bid")), "ask": _clean(r.get("ask"))} for _, r in df.iterrows()]
            out.append({"expiry": e, "calls": rows(ch.calls), "puts": rows(ch.puts)})
        return {"spot": _clean(spot), "expiries": out, "all_expiries": exps}
    return _cached(f"yahoo:options:{symbol}:{max_days}", "Yahoo opsiyon zinciri", timedelta(minutes=ttl_minutes), fn)


def stamp(f: Fetched | None, label: str | None = None) -> str:
    """'FMP · 15 Kas 14:02' style provenance text."""
    if not f or not f.fetched_at:
        return f"{label or (f.source if f else '?')} · alınamadı"
    d = parse_dt(f.fetched_at).astimezone(config.LOCAL_TZ)
    s = f"{label or f.source} · {d:%d.%m %H:%M}"
    return s + " (bayat)" if f.stale else s
