"""Personal portfolio ledger.

Sub-portfolios hold trades and cash flows. One replay engine turns the ledger
plus stored daily closes into: current positions (average cost), realized P/L,
closed round trips, per-trade running state and a daily value series with
time-weighted returns. Every number is computed here — nothing comes from a model.

Conventions
- Quantities are put on today's share basis using stored split events, so they
  line up with Yahoo's split-adjusted closes. Broker "split" rows are ignored.
- Money is reported in USD; non-USD trades/flows convert with the daily FX close.
- Sub-portfolio cash may go negative (a sub can spend another sub's cash). If the
  *total* cash would go negative (trades entered without deposits), the shortfall
  is booked as an implicit deposit so returns stay meaningful.
- Total return: time-weighted on total value, deposits/withdrawals are flows.
  Sub-portfolio return: time-weighted on its holdings, buys/sells are flows and
  dividends count as gain (the return of what the sub holds, independent of cash).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config, db, prices
from .util import parse_dt, utcnow, utcnow_iso, iso

log = logging.getLogger(__name__)
EPS = 1e-9
SIDES = ("BUY", "SELL")
ASSETS = ("stock", "etf", "option", "crypto")
CASH_KINDS = ("deposit", "withdrawal", "dividend", "interest", "fee", "transfer_in", "transfer_out", "adjustment")
EXTERNAL_KINDS = {"deposit", "withdrawal", "transfer_in", "transfer_out"}
KIND_SIGN = {"deposit": 1, "withdrawal": -1, "dividend": 1, "interest": 1, "fee": -1, "transfer_in": 1, "transfer_out": -1}
KIND_TR = {"deposit": "Para yatırma", "withdrawal": "Para çekme", "dividend": "Temettü", "interest": "Faiz",
           "fee": "Ücret", "transfer_in": "Transfer (giriş)", "transfer_out": "Transfer (çıkış)", "adjustment": "Düzeltme"}
OCC = re.compile(r"^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


class LedgerError(ValueError):
    pass


def ny_day(ts: str) -> str:
    return parse_dt(ts).astimezone(config.NY_TZ).date().isoformat()


def parse_occ(symbol: str) -> dict | None:
    m = OCC.match(symbol.upper())
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    return {"underlying": root, "expiry": f"20{yy}-{mm}-{dd}", "opt_type": cp, "strike": int(strike) / 1000}


# ── cache ─────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


# ── sub-portfolios ────────────────────────────────────────

def subportfolios(include_archived: bool = False) -> list[dict]:
    rows = db.query(f"SELECT * FROM subportfolios {'' if include_archived else 'WHERE archived = 0'} ORDER BY sort, code")
    return [dict(r) for r in rows]


def resolve_sub(sub) -> dict:
    if isinstance(sub, dict):
        return sub
    r = None
    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        r = db.query_one("SELECT * FROM subportfolios WHERE id = ?", (int(sub),))
    if r is None and isinstance(sub, str):
        r = db.query_one("SELECT * FROM subportfolios WHERE upper(code) = ? OR lower(name) = ?", (sub.upper(), sub.lower()))
    if r is None:
        raise LedgerError(f"alt portföy bulunamadı: {sub}")
    return dict(r)


def add_sub(code: str, name: str, color: str | None = None) -> int:
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,6}", code):
        raise LedgerError("kod 1-6 harf/rakam olmalı (örn. P3)")
    if db.query_one("SELECT 1 FROM subportfolios WHERE code = ?", (code,)):
        raise LedgerError(f"{code} zaten var")
    n = db.query_one("SELECT COALESCE(MAX(sort), -1) + 1 n FROM subportfolios")["n"]
    sid = db.execute("INSERT INTO subportfolios(code, name, color, sort, created_at) VALUES (?,?,?,?,?)",
                     (code, name.strip() or code, color, n, utcnow_iso()))
    invalidate()
    return sid


def update_sub(sub, *, name: str | None = None, color: str | None = None, archived: bool | None = None) -> None:
    s = resolve_sub(sub)
    db.execute("UPDATE subportfolios SET name = ?, color = ?, archived = ? WHERE id = ?",
               (name.strip() if name else s["name"], color if color is not None else s["color"],
                int(archived) if archived is not None else s["archived"], s["id"]))
    invalidate()


# ── writes ────────────────────────────────────────────────

def add_trade(*, side: str, symbol: str, qty: float, price: float, sub, ts: str | None = None, fees: float = 0,
              currency: str = "USD", asset: str | None = None, multiplier: float | None = None, note: str | None = None,
              source: str = "manual", ext_id: str | None = None) -> int | None:
    """Insert a trade. Returns the id, or None if `ext_id` was already imported."""
    side = side.upper().strip()
    symbol = symbol.upper().strip().replace(" ", "")
    if side not in SIDES:
        raise LedgerError("yön BUY veya SELL olmalı")
    if not symbol:
        raise LedgerError("sembol boş")
    qty, price, fees = float(qty), float(price), float(fees or 0)
    if qty <= 0 or price < 0 or fees < 0:
        raise LedgerError("adet > 0, fiyat ve ücret ≥ 0 olmalı")
    occ = parse_occ(symbol)
    asset = (asset or ("option" if occ else "crypto" if symbol.endswith(("-USD", "-EUR")) else "stock")).lower()
    if asset not in ASSETS:
        raise LedgerError(f"varlık türü {ASSETS} içinden olmalı")
    if multiplier is None:
        multiplier = 100.0 if asset == "option" else 1.0
    when = parse_dt(ts) if ts else utcnow()
    if when is None:
        raise LedgerError(f"tarih okunamadı: {ts}")
    s = resolve_sub(sub)
    with db.tx() as c:
        if ext_id and c.execute("SELECT 1 FROM trades WHERE ext_id = ?", (ext_id,)).fetchone():
            return None
        cur = c.execute(
            """INSERT INTO trades(ts, sub_id, symbol, asset, side, qty, price, fees, currency, multiplier,
                                  underlying, opt_type, strike, expiry, note, source, ext_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iso(when), s["id"], symbol, asset, side, qty, price, fees, currency.upper(), float(multiplier),
             occ and occ["underlying"], occ and occ["opt_type"], occ and occ["strike"], occ and occ["expiry"],
             (note or "").strip() or None, source, ext_id, utcnow_iso()),
        )
        tid = cur.lastrowid
    invalidate()
    return tid


def add_cash(*, kind: str, amount: float, sub, ts: str | None = None, currency: str = "USD", symbol: str | None = None,
             note: str | None = None, source: str = "manual", ext_id: str | None = None, signed: bool = False) -> int | None:
    """Insert a cash flow. Amount is signed by `kind` unless `signed=True` (then taken as-is)."""
    kind = kind.lower().strip()
    if kind not in CASH_KINDS:
        raise LedgerError(f"tür {CASH_KINDS} içinden olmalı")
    amount = float(amount)
    if not signed and kind in KIND_SIGN:
        amount = abs(amount) * KIND_SIGN[kind]
    when = parse_dt(ts) if ts else utcnow()
    if when is None:
        raise LedgerError(f"tarih okunamadı: {ts}")
    s = resolve_sub(sub)
    with db.tx() as c:
        if ext_id and c.execute("SELECT 1 FROM cash_flows WHERE ext_id = ?", (ext_id,)).fetchone():
            return None
        cur = c.execute(
            """INSERT INTO cash_flows(ts, sub_id, kind, amount, currency, symbol, note, source, ext_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (iso(when), s["id"], kind, amount, currency.upper(), symbol.upper() if symbol else None,
             (note or "").strip() or None, source, ext_id, utcnow_iso()),
        )
        cid = cur.lastrowid
    invalidate()
    return cid


def transfer(amount: float, from_sub, to_sub, ts: str | None = None, note: str | None = None) -> None:
    a, b = resolve_sub(from_sub), resolve_sub(to_sub)
    if a["id"] == b["id"]:
        raise LedgerError("aynı alt portföye transfer olmaz")
    add_cash(kind="transfer_out", amount=amount, sub=a, ts=ts, note=note or f"→ {b['code']}")
    add_cash(kind="transfer_in", amount=amount, sub=b, ts=ts, note=note or f"← {a['code']}")


def update_trade(trade_id: int, *, sub=None, note: str | None = None) -> None:
    t = db.query_one("SELECT * FROM trades WHERE id = ?", (trade_id,))
    if not t:
        raise LedgerError("işlem bulunamadı")
    sub_id = resolve_sub(sub)["id"] if sub not in (None, "") else t["sub_id"]
    db.execute("UPDATE trades SET sub_id = ?, note = ? WHERE id = ?",
               (sub_id, t["note"] if note is None else (note.strip() or None), trade_id))
    invalidate()


def move_symbol(symbol: str, sub) -> int:
    """Reassign every trade of a symbol to one sub-portfolio (handy after an import)."""
    n = db.execute("UPDATE trades SET sub_id = ? WHERE symbol = ?", (resolve_sub(sub)["id"], symbol.upper()))
    invalidate()
    return n


def delete_trade(trade_id: int) -> None:
    db.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    invalidate()


def delete_cash(cash_id: int) -> None:
    db.execute("DELETE FROM cash_flows WHERE id = ?", (cash_id,))
    invalidate()


def instruments() -> list[str]:
    return [r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM trades ORDER BY symbol")]


def price_symbols() -> list[str]:
    """Everything the portfolio needs priced: instruments, FX pairs, benchmarks, calendar."""
    syms = set(instruments()) | set(config.BENCHMARKS) | {config.CALENDAR_SYMBOL}
    for r in db.query("SELECT DISTINCT currency FROM trades UNION SELECT DISTINCT currency FROM cash_flows"):
        fx = prices.fx_symbol(r["currency"])
        if fx:
            syms.add(fx)
    return sorted(syms)


def first_day() -> str | None:
    r = db.query_one("SELECT MIN(ts) t FROM (SELECT ts FROM trades UNION ALL SELECT ts FROM cash_flows)")
    return ny_day(r["t"]) if r and r["t"] else None


def refresh_prices() -> dict[str, int]:
    start = first_day()
    st = date.fromisoformat(start) - timedelta(days=10) if start else None
    out = prices.refresh(price_symbols(), start=st)
    invalidate()
    return out


# ── replay engine ─────────────────────────────────────────

@dataclass
class Position:
    sub_id: int
    symbol: str
    asset: str
    currency: str
    multiplier: float
    qty: float = 0.0
    cost: float = 0.0           # USD basis of the remaining quantity
    realized: float = 0.0       # lifetime realized USD
    dividends: float = 0.0
    last_price: float | None = None   # last trade price (fallback when no close exists)
    trip: dict | None = None
    first_day: str | None = None
    last_day: str | None = None


@dataclass
class Ledger:
    subs: dict[int, dict]
    positions: dict[tuple[int, str], Position]
    cash: dict[int, float]
    implicit: dict[int, float]
    closed: list[dict]
    trade_state: dict[int, dict]
    days: list[str]
    total: list[dict]                       # per day: value, cash, flow, idx
    by_sub: dict[int, list[dict]]           # per day: holdings, cash, flow, div, idx
    closes: dict[str, prices.Series]
    warnings: list[str] = field(default_factory=list)
    built_at: float = 0.0


def _load_events() -> list[dict]:
    ev = [dict(r) | {"_type": "trade"} for r in db.query("SELECT * FROM trades")]
    ev += [dict(r) | {"_type": "cash"} for r in db.query("SELECT * FROM cash_flows")]
    for e in ev:
        e["_day"] = ny_day(e["ts"])
    # same timestamp: cash in before trades, buys before sells
    order = {"cash": 0, "BUY": 1, "SELL": 2}
    ev.sort(key=lambda e: (e["ts"], order.get(e.get("side") or e["_type"], 0), e["id"]))
    return ev


def build(force: bool = False) -> Ledger:
    with _cache_lock:
        hit = _cache.get("ledger")
        if hit and not force and time.time() - hit.built_at < 60:
            return hit
    led = _build()
    with _cache_lock:
        _cache["ledger"] = led
    return led


def _build() -> Ledger:
    subs = {s["id"]: s for s in subportfolios(include_archived=True)}
    events = _load_events()
    start = events[0]["_day"] if events else None
    syms = {e["symbol"] for e in events if e["_type"] == "trade"}
    curs = {e["currency"] for e in events} - {"USD"}
    closes = prices.series_many(syms | {prices.fx_symbol(c) for c in curs} | set(config.BENCHMARKS),
                                (date.fromisoformat(start) - timedelta(days=10)).isoformat() if start else None)
    split_cache: dict = {}
    warnings: list[str] = []

    def fx(currency: str, day: str) -> float:
        sym = prices.fx_symbol(currency)
        if not sym:
            return 1.0
        v = closes.get(sym) and closes[sym].at(day)
        if v is None:
            warnings.append(f"{currency} kuru yok ({day}); 1 kabul edildi")
            return 1.0
        return v

    positions: dict[tuple[int, str], Position] = {}
    cash = {sid: 0.0 for sid in subs}
    implicit = {sid: 0.0 for sid in subs}
    closed: list[dict] = []
    trade_state: dict[int, dict] = {}

    # daily accumulators, reset per day
    day_flow = 0.0
    sub_flow: dict[int, float] = {}
    sub_div: dict[int, float] = {}

    def apply(e: dict) -> None:
        nonlocal day_flow
        sid, day = e["sub_id"], e["_day"]
        cash.setdefault(sid, 0.0)
        implicit.setdefault(sid, 0.0)
        if e["_type"] == "cash":
            amt = e["amount"] * fx(e["currency"], day)
            cash[sid] += amt
            if e["kind"] in EXTERNAL_KINDS:
                if e["kind"] not in ("transfer_in", "transfer_out"):
                    day_flow += amt
            elif e["kind"] in ("dividend", "interest") or (e["kind"] == "adjustment" and e.get("symbol")):
                sub_div[sid] = sub_div.get(sid, 0.0) + amt
                if e.get("symbol"):
                    p = positions.get((sid, e["symbol"]))
                    if p:
                        p.dividends += amt
        else:
            sym = e["symbol"]
            key = (sid, sym)
            p = positions.get(key)
            if p is None:
                p = positions[key] = Position(sid, sym, e["asset"], e["currency"], e["multiplier"])
            f = prices.split_factor_after(sym, day, split_cache) if e["asset"] in ("stock", "etf") else 1.0
            q, unit = e["qty"] * f, e["price"] / f
            rate = fx(e["currency"], day)
            gross = q * unit * p.multiplier * rate
            fee = e["fees"] * rate
            st: dict = {}
            if e["side"] == "BUY":
                if p.qty <= EPS:
                    p.trip = {"sub_id": sid, "symbol": sym, "asset": p.asset, "open": day, "cost": 0.0, "proceeds": 0.0,
                              "realized": 0.0, "qty": 0.0, "buys": 0, "sells": 0, "entry_px": unit * rate,
                              "avg_down": 0, "add_up": 0, "max_cost": 0.0}
                elif p.trip:  # adding to an open position: below / above the running average?
                    avg = p.cost / (p.qty * p.multiplier)
                    if unit * rate < avg * 0.98:
                        p.trip["avg_down"] += 1
                    elif unit * rate > avg * 1.02:
                        p.trip["add_up"] += 1
                p.qty += q
                p.cost += gross + fee
                cash[sid] -= gross + fee
                sub_flow[sid] = sub_flow.get(sid, 0.0) + gross + fee
                p.trip["cost"] += gross + fee
                p.trip["qty"] += q
                p.trip["buys"] += 1
                p.trip["max_cost"] = max(p.trip.get("max_cost", 0.0), p.cost)
            else:
                if q > p.qty + 1e-6:
                    warnings.append(f"{sym}: {day} tarihinde eldekinden fazla satış ({q:g} > {p.qty:g}); fazlası yok sayıldı")
                    scale = (p.qty / q) if q else 0
                    q, gross, fee = p.qty, gross * scale, fee * scale
                if q > EPS:
                    basis = p.cost * (q / p.qty)
                    proceeds = gross - fee
                    realized = proceeds - basis
                    p.qty -= q
                    p.cost -= basis
                    p.realized += realized
                    cash[sid] += proceeds
                    sub_flow[sid] = sub_flow.get(sid, 0.0) - proceeds
                    st["realized"] = realized
                    st["realized_pct"] = realized / basis * 100 if basis > EPS else None
                    if p.trip:
                        p.trip["proceeds"] += proceeds
                        p.trip["realized"] += realized
                        p.trip["sells"] += 1
                    if p.qty <= 1e-7:
                        p.qty, p.cost = 0.0, 0.0
                        if p.trip:
                            t = p.trip
                            t["close"] = day
                            t["exit_px"] = unit * rate
                            t["days"] = (date.fromisoformat(day) - date.fromisoformat(t["open"])).days
                            t["pct"] = t["realized"] / t["cost"] * 100 if t["cost"] > EPS else None
                            closed.append(t)
                            p.trip = None
            p.last_price = unit * rate
            p.first_day = p.first_day or day
            p.last_day = day
            st["qty_after"] = p.qty
            st["avg_after"] = p.cost / (p.qty * p.multiplier) if p.qty > EPS else None
            st["split_factor"] = f
            trade_state[e["id"]] = st

    def settle() -> None:
        """Book an implicit deposit if total cash is negative after a whole day's events.
        Checked per day, not per event: brokers often stamp the auto top-up a second after the buy it funds."""
        nonlocal day_flow
        total_cash = sum(cash.values())
        if total_cash < -0.005:
            sid = min(cash, key=cash.get)
            cash[sid] -= total_cash
            implicit[sid] -= total_cash
            day_flow -= total_cash

    def price(p: Position, day: str) -> float | None:
        s = closes.get(p.symbol)
        v = s.at(day) if s else None
        if v is None:
            return p.last_price
        return v * fx(p.currency, day)

    days: list[str] = []
    if start:
        days = prices.trading_days(start)
        tail = events[-1]["_day"]
        if not days or days[-1] < tail:
            days.append(tail)
        if days[0] > start:
            days.insert(0, start)

    total_rows: list[dict] = []
    sub_rows: dict[int, list[dict]] = {sid: [] for sid in subs}
    i = 0
    prev_v: float | None = 0.0   # start from zero so the first day's move counts
    prev_hold: dict[int, float] = {}
    idx = 1.0
    sidx = {sid: 1.0 for sid in subs}
    for d in days:
        day_flow = 0.0
        sub_flow, sub_div = {}, {}
        while i < len(events) and events[i]["_day"] <= d:
            apply(events[i])
            i += 1
        settle()
        hold: dict[int, float] = {}
        for p in positions.values():
            if p.qty > EPS:
                px = price(p, d)
                hold[p.sub_id] = hold.get(p.sub_id, 0.0) + (px or 0.0) * p.qty * p.multiplier
        value = sum(hold.values()) + sum(cash.values())
        if prev_v is not None:
            base = prev_v + day_flow
            r = (value - base) / base if base > 0.01 else 0.0
            idx *= 1 + r
        total_rows.append({"day": d, "value": value, "cash": sum(cash.values()), "flow": day_flow, "idx": idx})
        prev_v = value
        for sid in set(subs) | set(hold) | set(cash):
            h = hold.get(sid, 0.0)
            nf = sub_flow.get(sid, 0.0)
            dv = sub_div.get(sid, 0.0)
            ph = prev_hold.get(sid, 0.0)
            base = ph + max(nf, 0.0)
            r = (h + dv - ph - nf) / base if base > 0.01 else 0.0
            sidx[sid] = sidx.get(sid, 1.0) * (1 + r)
            sub_rows.setdefault(sid, []).append({"day": d, "holdings": h, "cash": cash.get(sid, 0.0), "flow": nf,
                                                 "div": dv, "idx": sidx.get(sid, 1.0)})
            prev_hold[sid] = h
    while i < len(events):  # events dated after the last price day
        apply(events[i])
        i += 1
    settle()

    return Ledger(subs=subs, positions=positions, cash=cash, implicit=implicit, closed=closed,
                  trade_state=trade_state, days=days, total=total_rows, by_sub=sub_rows, closes=closes,
                  warnings=list(dict.fromkeys(warnings)), built_at=time.time())


# ── read models ───────────────────────────────────────────

def _start_index(rows: list[dict], period: str) -> int | None:
    """Index of the row whose value is the period's starting point."""
    if not rows:
        return None
    last = date.fromisoformat(rows[-1]["day"])
    if period == "all":
        return 0
    if period == "day":
        return max(len(rows) - 2, 0)
    if period == "week":
        return max(len(rows) - 6, 0)
    if period == "mtd":
        cut = last.replace(day=1)
    elif period == "ytd":
        cut = last.replace(month=1, day=1)
    elif period == "m1":
        cut = last - timedelta(days=30) + timedelta(days=1)
    elif period == "m3":
        cut = last - timedelta(days=91) + timedelta(days=1)
    elif period == "y1":
        cut = last - timedelta(days=365) + timedelta(days=1)
    else:
        raise ValueError(period)
    c = cut.isoformat()
    j = None
    for k, r in enumerate(rows):
        if r["day"] < c:
            j = k
        else:
            break
    return j if j is not None else 0


PERIODS = [("day", "Günlük"), ("week", "Hafta (5 işlem günü)"), ("mtd", "Ay başından"), ("m1", "Son 1 ay"),
           ("ytd", "Yıl başından"), ("y1", "Son 1 yıl"), ("all", "Tüm zamanlar")]


def _bench(led: Ledger, d0: str, d1: str) -> dict[str, float | None]:
    out = {}
    for sym, label in config.BENCHMARKS.items():
        s = led.closes.get(sym)
        a, b = (s.at(d0), s.at(d1)) if s else (None, None)
        out[label] = (b / a - 1) * 100 if a and b else None
    return out


def total_perf(led: Ledger | None = None) -> dict:
    led = led or build()
    rows = led.total
    out: dict = {"periods": {}, "as_of": rows[-1]["day"] if rows else None}
    if not rows:
        return out
    end = rows[-1]
    for key, _ in PERIODS:
        j = _start_index(rows, key)
        s = rows[j]
        flows = sum(r["flow"] for r in rows[j + 1:])
        if key == "all":
            flows = sum(r["flow"] for r in rows)
            pnl = end["value"] - flows
            pct = (end["idx"] - 1) * 100
        else:
            pnl = end["value"] - s["value"] - flows
            pct = (end["idx"] / s["idx"] - 1) * 100 if s["idx"] else None
        out["periods"][key] = {"pct": pct, "pnl": pnl, "from": s["day"], "bench": _bench(led, s["day"], end["day"])}
    first = rows[0]["day"]
    years = (date.fromisoformat(end["day"]) - date.fromisoformat(first)).days / 365.25
    out["years"] = years
    out["start_day"] = first
    out["cagr"] = (end["idx"] ** (1 / years) - 1) * 100 if years >= 0.25 and end["idx"] > 0 else None
    out["bench_cagr"] = {}
    for sym, label in config.BENCHMARKS.items():
        s = led.closes.get(sym)
        a, b = (s.at(first), s.at(end["day"])) if s else (None, None)
        out["bench_cagr"][label] = ((b / a) ** (1 / years) - 1) * 100 if a and b and years >= 0.25 else None
    ath = max(rows, key=lambda r: r["value"])
    out["ath"] = {"value": ath["value"], "day": ath["day"],
                  "dd_pct": (end["value"] / ath["value"] - 1) * 100 if ath["value"] > 0 else None}
    out["value"] = end["value"]
    out["cash"] = end["cash"]
    out["deposits"] = sum(r["flow"] for r in rows)
    prev_year_end = next((r for r in reversed(rows) if r["day"] < f"{end['day'][:4]}-01-01"), None)
    out["prev_year_close"] = {"day": prev_year_end["day"], "value": prev_year_end["value"]} if prev_year_end else None
    out["prev_close"] = {"day": rows[-2]["day"], "value": rows[-2]["value"]} if len(rows) > 1 else None
    out["implicit_funding"] = sum(led.implicit.values())
    return out


def sub_perf(sub_id: int, led: Ledger | None = None) -> dict:
    led = led or build()
    rows = led.by_sub.get(sub_id) or []
    out: dict = {"periods": {}}
    if not rows:
        return out
    end = rows[-1]
    for key in ("day", "week", "mtd", "ytd", "all"):
        j = _start_index(rows, key)
        s = rows[j]
        tail = rows[j + 1:] if key != "all" else rows
        flow = sum(r["flow"] for r in tail)
        div = sum(r["div"] for r in tail)
        base_h = 0.0 if key == "all" else s["holdings"]
        pnl = end["holdings"] - base_h - flow + div
        idx0 = 1.0 if key == "all" else s["idx"]
        out["periods"][key] = {"pct": (end["idx"] / idx0 - 1) * 100 if idx0 else None, "pnl": pnl, "from": s["day"]}
    return out


def positions(sub_id: int | None = None, led: Ledger | None = None) -> list[dict]:
    led = led or build()
    day = led.days[-1] if led.days else None
    prev = led.days[-2] if len(led.days) > 1 else None
    total_value = led.total[-1]["value"] if led.total else 0.0
    out = []
    for p in led.positions.values():
        if p.qty <= EPS or (sub_id is not None and p.sub_id != sub_id):
            continue
        s = led.closes.get(p.symbol)
        last = s.last() if s else None
        px = (s.at(day) if s and day else None)
        stale = px is None
        if px is None:
            px = p.last_price
        pxp = s.at(prev) if s and prev else None
        value = (px or 0) * p.qty * p.multiplier
        avg = p.cost / (p.qty * p.multiplier)
        out.append({
            "sub_id": p.sub_id, "sub": led.subs.get(p.sub_id, {}).get("code"), "symbol": p.symbol, "asset": p.asset,
            "qty": p.qty, "multiplier": p.multiplier, "avg": avg, "cost": p.cost, "price": px,
            "price_day": last[0] if last else None, "stale": stale,
            "day_pct": (px / pxp - 1) * 100 if px and pxp else None,
            "day_pnl": (px - pxp) * p.qty * p.multiplier if px and pxp else None,
            "value": value, "unreal": value - p.cost, "unreal_pct": (value / p.cost - 1) * 100 if p.cost > EPS else None,
            "weight": value / total_value * 100 if total_value > 0 else None,
            "realized": p.realized, "dividends": p.dividends, "first_day": p.trip["open"] if p.trip else p.first_day,
            "last_day": p.last_day,
        })
    out.sort(key=lambda r: -r["value"])
    return out


def allocation(led: Ledger | None = None) -> list[dict]:
    led = led or build()
    pos = positions(led=led)
    total = led.total[-1]["value"] if led.total else 0.0
    out = []
    for sid, s in led.subs.items():
        if s["archived"]:
            continue
        pv = sum(p["value"] for p in pos if p["sub_id"] == sid)
        c = led.cash.get(sid, 0.0)
        out.append({"sub": s, "positions": pv, "cash": c, "total": pv + c, "n": sum(1 for p in pos if p["sub_id"] == sid),
                    "share": (pv + c) / total * 100 if total > 0 else None, "perf": sub_perf(sid, led)})
    return out


def trades(*, asset: str | None = None, symbol: str | None = None, sub_id: int | None = None,
           limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    led = build()
    where, params = [], []
    if asset == "option":
        where.append("t.asset = 'option'")
    elif asset == "stock":
        where.append("t.asset IN ('stock','etf')")
    elif asset == "crypto":
        where.append("t.asset = 'crypto'")
    if symbol:
        where.append("(t.symbol = ? OR t.underlying = ?)")
        params += [symbol.upper(), symbol.upper()]
    if sub_id:
        where.append("t.sub_id = ?")
        params.append(sub_id)
    w = f"WHERE {' AND '.join(where)}" if where else ""
    n = db.query_one(f"SELECT COUNT(*) n FROM trades t {w}", params)["n"]
    rows = db.query(f"""SELECT t.*, s.code sub_code, s.name sub_name FROM trades t JOIN subportfolios s ON s.id = t.sub_id
                        {w} ORDER BY t.ts DESC, t.id DESC LIMIT ? OFFSET ?""", params + [limit, offset])
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = r["qty"] * r["price"] * r["multiplier"]
        d.update(led.trade_state.get(r["id"], {}))
        out.append(d)
    return out, n


def cash_flows(limit: int = 100) -> list[dict]:
    rows = db.query("""SELECT c.*, s.code sub_code FROM cash_flows c JOIN subportfolios s ON s.id = c.sub_id
                       ORDER BY c.ts DESC, c.id DESC LIMIT ?""", (limit,))
    return [dict(r) | {"kind_tr": KIND_TR.get(r["kind"], r["kind"])} for r in rows]


def month_stats(month: str | None = None) -> dict:
    """Trade activity for a calendar month (YYYY-MM, NY dates)."""
    led = build()
    month = month or utcnow().astimezone(config.NY_TZ).strftime("%Y-%m")
    rows = [dict(r) for r in db.query("SELECT * FROM trades WHERE ts >= ? ORDER BY ts",
                                      ((date.fromisoformat(month + "-01") - timedelta(days=1)).isoformat(),))]
    rows = [r for r in rows if ny_day(r["ts"])[:7] == month]
    buys = [r for r in rows if r["side"] == "BUY"]
    sells = [r for r in rows if r["side"] == "SELL"]
    realized = sum(led.trade_state.get(r["id"], {}).get("realized") or 0 for r in sells)
    counts: dict[str, int] = {}
    for r in rows:
        k = r["underlying"] or r["symbol"]
        counts[k] = counts.get(k, 0) + 1
    top = max(counts.items(), key=lambda kv: kv[1]) if counts else None
    return {"month": month, "n": len(rows), "buys": len(buys), "sells": len(sells), "realized": realized,
            "buy_amount": sum(r["qty"] * r["price"] * r["multiplier"] for r in buys),
            "sell_amount": sum(r["qty"] * r["price"] * r["multiplier"] for r in sells),
            "top": top}


def recent_summary(days: int = 7) -> dict:
    led = build()
    since = iso(utcnow() - timedelta(days=days))
    rows = [dict(r) for r in db.query("SELECT * FROM trades WHERE ts >= ?", (since,))]
    buys = [r for r in rows if r["side"] == "BUY"]
    sells = [r for r in rows if r["side"] == "SELL"]
    return {"days": days, "n": len(rows), "buys": len(buys), "sells": len(sells),
            "buy_amount": sum(r["qty"] * r["price"] * r["multiplier"] for r in buys),
            "sell_amount": sum(r["qty"] * r["price"] * r["multiplier"] for r in sells),
            "realized": sum(led.trade_state.get(r["id"], {}).get("realized") or 0 for r in sells)}


def closed_positions(led: Ledger | None = None) -> list[dict]:
    led = led or build()
    out = [dict(t) | {"sub": led.subs.get(t["sub_id"], {}).get("code")} for t in led.closed]
    out.sort(key=lambda t: t["close"], reverse=True)
    return out


def chart(range_: str = "ytd", sub_id: int | None = None) -> dict:
    """Return-index series (percent since range start) for the portfolio and benchmarks."""
    led = build()
    rows = led.by_sub.get(sub_id, []) if sub_id else led.total
    if not rows:
        return {"portfolio": [], "bench": {}, "value": []}
    period = {"1m": "m1", "3m": "m3", "ytd": "ytd", "1y": "y1", "all": "all", "mtd": "mtd"}.get(range_, "ytd")
    j = _start_index(rows, period)
    win = rows[j:]
    base = win[0]["idx"]
    out = {"portfolio": [{"time": r["day"], "value": (r["idx"] / base - 1) * 100} for r in win],
           "value": [{"time": r["day"], "value": r.get("value", r.get("holdings"))} for r in win], "bench": {}}
    for sym, label in config.BENCHMARKS.items():
        s = led.closes.get(sym)
        b0 = s.at(win[0]["day"]) if s else None
        if b0:
            out["bench"][label] = [{"time": r["day"], "value": (s.at(r["day"]) / b0 - 1) * 100}
                                   for r in win if s.at(r["day"])]
    return out


def headline(perf: dict | None = None) -> str | None:
    """Deterministic one-liner for the page header (no model)."""
    perf = perf or total_perf()
    w = perf.get("periods", {}).get("week")
    m = perf.get("periods", {}).get("mtd")
    if not w or w["pct"] is None:
        return None
    word = "yukarıda" if w["pct"] >= 0 else "aşağıda"
    s = f"Portföy bu hafta %{abs(w['pct']):.2f} {word}"
    spx = (m or {}).get("bench", {}).get("SPX")
    if m and m["pct"] is not None and spx is not None:
        diff = m["pct"] - spx
        s += f"; ay başından beri %{m['pct']:+.2f} ile SPX'in {abs(diff):.1f} puan {'önünde' if diff >= 0 else 'gerisinde'}"
    return s + "."
