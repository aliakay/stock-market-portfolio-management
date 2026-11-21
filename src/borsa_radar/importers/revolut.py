"""Revolut (stock trading) account statement CSV → ledger.

Export in the Revolut app: Invest → ··· → Documents → Brokerage account →
Account statement → Excel/CSV. Expected header (column order may vary):

    Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate

Re-importing the same file is safe: every row gets a stable `ext_id`.
Split rows are skipped (quantities are put on today's basis from Yahoo splits).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import portfolio
from ..util import parse_dt, sha

SOURCE = "revolut_csv"
ALIASES = {
    "date": ("date", "date (utc)", "time", "completed date"),
    "ticker": ("ticker", "symbol"),
    "type": ("type", "transaction type"),
    "qty": ("quantity", "qty", "shares"),
    "price": ("price per share", "price", "share price"),
    "total": ("total amount", "amount", "total"),
    "currency": ("currency",),
    "fx": ("fx rate", "fx"),
}
_SYM = {"$": "USD", "€": "EUR", "£": "GBP"}
_NUM = re.compile(r"-?\d[\d,]*\.?\d*|-?\.\d+")


def _money(raw: str | None) -> tuple[float | None, str | None]:
    """'USD 62.54' / '$62.54' / '-€0.50' / '1,234.56' → (value, currency)."""
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    cur = None
    m = re.search(r"\b([A-Z]{3})\b", s)
    if m:
        cur = m.group(1)
    for sym, code in _SYM.items():
        if sym in s:
            cur = cur or code
    neg = "-" in s or "(" in s
    body = re.sub(r"[A-Z]{3}|[$€£()\s]", "", s).lstrip("-")
    if "," in body and "." not in body and re.fullmatch(r"\d+,\d{1,2}", body):
        body = body.replace(",", ".")       # decimal comma
    body = body.replace(",", "")
    m = _NUM.search(body)
    if not m:
        return None, cur
    v = float(m.group(0))
    return (-abs(v) if neg else v), cur


def _date(raw: str) -> str | None:
    s = (raw or "").strip()
    d = parse_dt(s)
    if d is None:
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
            try:
                d = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    return d.isoformat() if d else None


def yahoo_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    if re.fullmatch(r"[A-Z]+\.[A-Z]", t):   # class shares: BRK.B → BRK-B
        t = t.replace(".", "-")
    return t


def _classify(type_: str) -> str:
    t = type_.upper()
    if "SPLIT" in t:
        return "split"
    if any(k in t for k in ("MERGER", "SPINOFF", "SPIN-OFF", "CORPORATE ACTION")):
        return "corp_cash" if "CASH" in t else "corp_stock"
    if "DIVIDEND" in t:
        return "dividend"
    if t.startswith("BUY") or " BUY" in t:
        return "buy"
    if t.startswith("SELL") or " SELL" in t:
        return "sell"
    if "TOP-UP" in t or "TOP UP" in t or "TOPUP" in t or "DEPOSIT" in t:
        return "deposit"
    if "WITHDRAW" in t:
        return "withdrawal"
    if "FEE" in t:
        return "fee"
    if "INTEREST" in t:
        return "interest"
    if "TRANSFER" in t:
        return "transfer"
    return "unknown"


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Parse CSV text into normalized rows. Returns (rows, header_problems)."""
    text = text.lstrip("﻿")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
    col = {}
    for key, names in ALIASES.items():
        col[key] = next((headers[n] for n in names if n in headers), None)
    problems = [k for k in ("date", "type", "total") if not col[k]]
    if problems:
        return [], [f"beklenen sütun yok: {', '.join(problems)} (bulunan: {', '.join(reader.fieldnames or [])})"]
    out = []
    seen: dict[str, int] = {}
    for n, raw in enumerate(reader, start=2):
        get = lambda k: (raw.get(col[k]) or "").strip() if col[k] else ""  # noqa: E731
        kind = _classify(get("type"))
        total, cur_t = _money(get("total"))
        price, cur_p = _money(get("price"))
        qty, _ = _money(get("qty"))
        fx, _ = _money(get("fx"))
        currency = (get("currency") or cur_t or cur_p or "USD").upper()
        ts = _date(get("date"))
        key = sha(get("date"), get("ticker"), get("type"), get("qty"), get("total"))
        seen[key] = seen.get(key, 0) + 1
        out.append({"line": n, "kind": kind, "type": get("type"), "ts": ts, "ticker": get("ticker"),
                    "qty": qty, "price": price, "total": total, "currency": currency, "fx": fx,
                    "ext_id": f"revolut:{key}:{seen[key]}"})
    return out, []


def _held(symbol: str, ts: str) -> float:
    from .. import db
    r = db.query_one("SELECT COALESCE(SUM(CASE side WHEN 'BUY' THEN qty ELSE -qty END), 0) q FROM trades "
                     "WHERE symbol = ? AND ts < ?", (symbol, ts))
    return r["q"] if r else 0.0


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
                if not r["ticker"] or not r["qty"]:
                    res["errors"].append(f"satır {r['line']}: {r['type']} için sembol/adet yok")
                    continue
                qty = abs(r["qty"])
                total = abs(r["total"] or 0)
                price = abs(r["price"]) if r["price"] else (total / qty if qty else 0)
                gross = qty * price
                fee = total - gross if k == "buy" else gross - total
                fee = round(fee, 2) if fee > 0.01 else 0.0
                sym = yahoo_symbol(r["ticker"])
                res["symbols"].add(sym)
                if dry_run:
                    res["trades"] += 1
                    continue
                tid = portfolio.add_trade(side="BUY" if k == "buy" else "SELL", symbol=sym, qty=qty, price=price, fees=fee,
                                          currency=r["currency"], sub=target, ts=r["ts"], source=SOURCE, ext_id=r["ext_id"])
                res["trades" if tid else "duplicates"] += 1
            elif k in ("deposit", "withdrawal", "dividend", "fee", "interest"):
                if r["total"] is None:
                    res["errors"].append(f"satır {r['line']}: tutar okunamadı")
                    continue
                amount = r["total"]
                signed = k == "dividend"      # dividend tax corrections arrive negative
                if dry_run:
                    res["cash"] += 1
                    continue
                cid = portfolio.add_cash(kind=k, amount=amount, sub=target, ts=r["ts"], currency=r["currency"],
                                         symbol=yahoo_symbol(r["ticker"]) if r["ticker"] else None, note=r["type"],
                                         source=SOURCE, ext_id=r["ext_id"], signed=signed)
                res["cash" if cid else "duplicates"] += 1
            elif k == "corp_cash":
                # cash paid out by a merger / rights cash-out: income, not a deposit
                if r["total"] is None:
                    res["errors"].append(f"satır {r['line']}: tutar okunamadı")
                    continue
                if dry_run:
                    res["cash"] += 1
                    continue
                cid = portfolio.add_cash(kind="adjustment", amount=r["total"], sub=target, ts=r["ts"], currency=r["currency"],
                                         symbol=yahoo_symbol(r["ticker"]) if r["ticker"] else None, note=r["type"],
                                         source=SOURCE, ext_id=r["ext_id"], signed=True)
                res["cash" if cid else "duplicates"] += 1
            elif k == "corp_stock" and r["ticker"] and r["qty"]:
                # shares added (+) or removed (−) by a corporate action, at zero price;
                # removals of something never held (e.g. rights that were cashed out) are ignored
                sym = yahoo_symbol(r["ticker"])
                held = _held(sym, r["ts"])
                if r["qty"] < 0 and held <= 1e-9:
                    res["skipped"][r["type"]] = res["skipped"].get(r["type"], 0) + 1
                    continue
                if dry_run:
                    res["trades"] += 1
                    continue
                tid = portfolio.add_trade(side="BUY" if r["qty"] > 0 else "SELL", symbol=sym,
                                          qty=min(abs(r["qty"]), held) if r["qty"] < 0 else r["qty"], price=0,
                                          currency=r["currency"], sub=target, ts=r["ts"], note=r["type"],
                                          source=SOURCE, ext_id=r["ext_id"])
                res["trades" if tid else "duplicates"] += 1
            else:
                res["skipped"][r["type"]] = res["skipped"].get(r["type"], 0) + 1
                if k in ("unknown", "transfer"):
                    res["unknown"].append(f"satır {r['line']}: {r['type']} {r['ticker']} {r['qty'] or ''} {r['total'] or ''}".strip())
        except portfolio.LedgerError as exc:
            res["errors"].append(f"satır {r['line']}: {exc}")
    res["symbols"] = sorted(res["symbols"])
    return res


def import_file(path: str | Path, sub, *, dry_run: bool = False) -> dict:
    return import_text(Path(path).read_text(encoding="utf-8-sig"), sub, dry_run=dry_run)
