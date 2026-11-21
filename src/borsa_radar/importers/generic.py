"""Broker-neutral trade history CSV → ledger, for anyone whose broker is not Revolut.

One row per trade or cash movement. Header names are case-insensitive; common broker spellings are accepted:

    date,type,symbol,quantity,price,fees,amount,currency
    2025-01-15,BUY,AAPL,10,185.20,1.00,,USD
    2025-02-03,SELL,AAPL,4,192.10,1.00,,USD
    2025-01-02,DEPOSIT,,,,,5000,USD
    2025-03-10,DIVIDEND,AAPL,,,,2.40,USD

- type: BUY / SELL (also AL / SAT), DEPOSIT, WITHDRAWAL, DIVIDEND, INTEREST, FEE.
- BUY / SELL need symbol, quantity and price; fees are optional. Symbols are Yahoo symbols (BRK.B is turned into BRK-B).
- Cash rows need amount (sign does not matter; the type decides it).
- date: 2025-01-15, 2025-01-15 15:30, 2025-01-15T15:30:00Z, 15.01.2025 or 15/01/2025.
Re-importing the same file is safe: every row gets a stable id from its contents.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .. import portfolio
from ..util import sha
from .revolut import _date, _money, yahoo_symbol

SOURCE = "csv"
ALIASES = {
    "date": ("date", "trade date", "datetime", "time", "tarih"),
    "type": ("type", "action", "side", "transaction type", "tür", "islem", "işlem"),
    "symbol": ("symbol", "ticker", "sembol"),
    "qty": ("quantity", "qty", "shares", "adet"),
    "price": ("price", "price per share", "fiyat"),
    "fees": ("fees", "fee", "commission", "komisyon"),
    "amount": ("amount", "total", "net amount", "tutar"),
    "currency": ("currency", "para birimi"),
}
KINDS = {"BUY": "buy", "AL": "buy", "BOUGHT": "buy", "SELL": "sell", "SAT": "sell", "SOLD": "sell",
         "DEPOSIT": "deposit", "WITHDRAWAL": "withdrawal", "WITHDRAW": "withdrawal", "DIVIDEND": "dividend",
         "INTEREST": "interest", "FEE": "fee"}
TEMPLATE = "date,type,symbol,quantity,price,fees,amount,currency\n"


def _columns(fieldnames) -> dict[str, str | None]:
    headers = {(h or "").strip().lower(): h for h in (fieldnames or [])}
    return {k: next((headers[n] for n in names if n in headers), None) for k, names in ALIASES.items()}


def looks_like(text: str) -> bool:
    """True when the header has this format's required columns (and is not a Revolut statement)."""
    head = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    try:
        dialect = csv.Sniffer().sniff(head, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    cols = _columns(next(csv.reader([head], dialect), []))
    return bool(cols["date"] and cols["type"] and cols["symbol"] and cols["qty"] and cols["price"]) and "total amount" not in head.lower()


def parse(text: str) -> tuple[list[dict], list[str]]:
    """PURE: CSV text → normalized rows; (rows, header problems)."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    col = _columns(reader.fieldnames)
    missing = [k for k in ("date", "type") if not col[k]]
    if missing:
        return [], [f"beklenen sütun yok: {', '.join(missing)} (bulunan: {', '.join(reader.fieldnames or [])}). Şablon: {TEMPLATE.strip()}"]
    out, seen = [], {}
    for n, raw in enumerate(reader, start=2):
        get = lambda k: (raw.get(col[k]) or "").strip() if col[k] else ""  # noqa: E731
        if not any((v or "").strip() for v in raw.values()):
            continue
        t = get("type").upper()
        kind = KINDS.get(t) or KINDS.get(t.split()[0] if t else "", "unknown")
        key = sha(*(get(k) for k in ALIASES))
        seen[key] = seen.get(key, 0) + 1
        out.append({"line": n, "kind": kind, "type": get("type"), "ts": _date(get("date")), "symbol": get("symbol"),
                    "qty": _money(get("qty"))[0], "price": _money(get("price"))[0], "fees": _money(get("fees"))[0],
                    "amount": _money(get("amount"))[0], "currency": (get("currency") or "USD").upper(),
                    "ext_id": f"csv:{key}:{seen[key]}"})
    return out, []


def import_text(text: str, sub, *, dry_run: bool = False) -> dict:
    rows, problems = parse(text)
    res = {"rows": len(rows), "trades": 0, "cash": 0, "duplicates": 0, "skipped": {}, "unknown": [], "errors": list(problems),
           "symbols": set(), "first": None, "last": None}
    target = portfolio.resolve_sub(sub)
    for r in rows:
        if not r["ts"]:
            res["errors"].append(f"satır {r['line']}: tarih okunamadı")
            continue
        res["first"] = min(res["first"] or r["ts"], r["ts"])
        res["last"] = max(res["last"] or r["ts"], r["ts"])
        k = r["kind"]
        try:
            if k in ("buy", "sell"):
                if not r["symbol"] or not r["qty"] or r["price"] is None:
                    res["errors"].append(f"satır {r['line']}: {r['type']} için sembol, adet ve fiyat gerekli")
                    continue
                sym = yahoo_symbol(r["symbol"])
                res["symbols"].add(sym)
                if dry_run:
                    res["trades"] += 1
                    continue
                tid = portfolio.add_trade(side=k.upper(), symbol=sym, qty=abs(r["qty"]), price=abs(r["price"]), fees=abs(r["fees"] or 0),
                                          currency=r["currency"], sub=target, ts=r["ts"], source=SOURCE, ext_id=r["ext_id"])
                res["trades" if tid else "duplicates"] += 1
            elif k in ("deposit", "withdrawal", "dividend", "interest", "fee"):
                if r["amount"] is None:
                    res["errors"].append(f"satır {r['line']}: {r['type']} için tutar (amount) gerekli")
                    continue
                if dry_run:
                    res["cash"] += 1
                    continue
                cid = portfolio.add_cash(kind=k, amount=r["amount"], sub=target, ts=r["ts"], currency=r["currency"],
                                         symbol=yahoo_symbol(r["symbol"]) if r["symbol"] else None, note=r["type"],
                                         source=SOURCE, ext_id=r["ext_id"])
                res["cash" if cid else "duplicates"] += 1
            else:
                res["unknown"].append(f"satır {r['line']}: {r['type'] or '(boş tür)'}")
        except portfolio.LedgerError as exc:
            res["errors"].append(f"satır {r['line']}: {exc}")
    return res


def import_file(path: str | Path, sub, *, dry_run: bool = False) -> dict:
    return import_text(Path(path).read_text(encoding="utf-8-sig"), sub, dry_run=dry_run)
