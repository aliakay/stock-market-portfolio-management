"""Watchlist management (shared by CLI, web UI and the Hermes skill)."""

from __future__ import annotations

from . import db, market
from .collectors import sec
from .util import utcnow_iso


class TickerError(ValueError):
    pass


def add(symbol: str, notes: str | None = None) -> dict:
    symbol = symbol.upper().strip().lstrip("$")
    if not symbol or len(symbol) > 12:
        raise TickerError("geçersiz sembol")
    existing = db.get_ticker(symbol)
    if existing:
        if not existing["active"]:
            db.execute("UPDATE tickers SET active = 1 WHERE symbol = ?", (symbol,))
            return {"symbol": symbol, "status": "reactivated", "name": existing["name"]}
        return {"symbol": symbol, "status": "exists", "name": existing["name"]}
    info = market.lookup(symbol)
    if not info:
        raise TickerError(f"{symbol} Yahoo Finance'te bulunamadı")
    cik = sec.cik_for(symbol)
    db.execute(
        """INSERT INTO tickers(symbol, name, short_name, sector, cik, active, notes, added_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (symbol, info["name"], info["short_name"], info["sector"], cik, notes, utcnow_iso()),
    )
    return {"symbol": symbol, "status": "added", "name": info["name"], "price": info["price"]}


def remove(symbol: str, *, purge: bool = False) -> dict:
    symbol = symbol.upper().strip().lstrip("$")
    if not db.get_ticker(symbol):
        raise TickerError(f"{symbol} listede yok")
    if purge:
        db.execute("DELETE FROM tickers WHERE symbol = ?", (symbol,))
        return {"symbol": symbol, "status": "deleted"}
    db.execute("UPDATE tickers SET active = 0 WHERE symbol = ?", (symbol,))
    return {"symbol": symbol, "status": "deactivated"}


def set_options(symbol: str, *, threshold: int | None = None, notes: str | None = None, clear_threshold: bool = False) -> dict:
    symbol = symbol.upper()
    if not db.get_ticker(symbol):
        raise TickerError(f"{symbol} listede yok")
    if clear_threshold:
        db.execute("UPDATE tickers SET alert_threshold = NULL WHERE symbol = ?", (symbol,))
    elif threshold is not None:
        if not 1 <= threshold <= 5:
            raise TickerError("eşik 1-5 arası olmalı")
        db.execute("UPDATE tickers SET alert_threshold = ? WHERE symbol = ?", (threshold, symbol))
    if notes is not None:
        db.execute("UPDATE tickers SET notes = ? WHERE symbol = ?", (notes, symbol))
    return dict(db.get_ticker(symbol))


def seed_defaults(symbols: list[str]) -> list[dict]:
    out = []
    if db.query_one("SELECT 1 FROM tickers"):
        return out
    for s in symbols:
        try:
            out.append(add(s))
        except TickerError as exc:
            out.append({"symbol": s, "status": "error", "error": str(exc)})
    return out


def sync_holdings() -> list[str]:
    """Put every held stock on the watchlist (news, SEC, alerts) unless the user switched it off before.
    Controlled by the `watch_holdings` setting."""
    if db.get_setting("watch_holdings") == "0":
        return []
    from . import portfolio
    added = []
    for p in portfolio.positions():
        if p["asset"] != "stock" or db.get_ticker(p["symbol"]):
            continue  # already watched, or deliberately paused (inactive rows are left alone)
        try:
            add(p["symbol"], notes="portföy (otomatik)")
            added.append(p["symbol"])
        except TickerError as exc:
            import logging
            logging.getLogger(__name__).warning("watch %s: %s", p["symbol"], exc)
    return added
