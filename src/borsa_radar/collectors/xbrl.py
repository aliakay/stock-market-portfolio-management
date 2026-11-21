"""SEC XBRL company facts → quarterly fundamentals; new quarter → analyzed event (YoY growth, dilution)."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date

from .. import db
from ..util import iso, sha, utcnow
from .sec import _get, cik_for

log = logging.getLogger(__name__)

FLOW = {
    "revenue": ["RevenuesNetOfInterestExpense", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "gross_profit": ["GrossProfit"],
    "op_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
INSTANT = {"cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]}
FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F"}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _units(fact: dict) -> list[dict]:
    u = fact.get("units") or {}
    return u.get("USD") or u.get("USD/shares") or next(iter(u.values()), [])


def _quarters_one(f: dict, is_eps: bool) -> dict[str, tuple[float, dict, str]]:
    """end -> (value from latest filing, entry, accession of the ORIGINAL filing for that period)."""
    q: dict[str, tuple[float, dict]] = {}
    fy: dict[str, tuple[float, dict]] = {}
    first_accn: dict[str, tuple[str, str]] = {}
    for e in _units(f):
        if e.get("form") not in FORMS or "start" not in e:
            continue
        d = _days(e["start"], e["end"])
        target = q if 80 <= d <= 100 else fy if 350 <= d <= 380 else None
        if target is None:
            continue
        prev = target.get(e["end"])
        if not prev or e["filed"] > prev[1]["filed"]:
            target[e["end"]] = (e["val"], e)
        fa = first_accn.get(e["end"])
        if not fa or e["filed"] < fa[0]:
            first_accn[e["end"]] = (e["filed"], e["accn"])
    for end, (val, e) in fy.items():
        if end in q or is_eps:
            continue
        inside = [v for k, (v, x) in q.items() if 0 < _days(k, end) < 300 and _days(e["start"], k) > 60]
        if len(inside) == 3:
            q[end] = (val - sum(inside), e)
    return {k: (v, e, first_accn.get(k, ("", e["accn"]))[1]) for k, (v, e) in q.items()}


def _quarters(facts: dict, names: list[str]) -> dict[str, tuple[float, dict, str]]:
    """Merge concepts by priority per period (companies switch concepts over time)."""
    out: dict[str, tuple[float, dict, str]] = {}
    is_eps = names is FLOW["eps_diluted"]
    for name in names:
        f = facts.get(name)
        if not f:
            continue
        for end, v in _quarters_one(f, is_eps).items():
            out.setdefault(end, v)
    return out


def _instant(facts: dict, names: list[str]) -> dict[str, float]:
    for name in names:
        f = facts.get(name)
        if not f:
            continue
        out = {}
        for e in _units(f):
            if e.get("form") in FORMS and "start" not in e:
                out[e["end"]] = e["val"]
        if out:
            return out
    return {}


def _shares(dei: dict) -> dict[str, float]:
    """Cover-page shares outstanding keyed by the accession's report period (approx.: by accn)."""
    f = dei.get("EntityCommonStockSharesOutstanding")
    out: dict[str, float] = defaultdict(float)
    if not f:
        return out
    by_accn: dict[str, float] = defaultdict(float)
    for e in _units(f):
        if e.get("form") in FORMS:
            by_accn[e["accn"]] += e["val"]  # multiple share classes are summed
    return by_accn


def refresh_ticker(ticker) -> int:
    sym = ticker["symbol"]
    cik = ticker["cik"] or cik_for(sym)
    if not cik:
        return 0
    try:
        data = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json").json()
    except Exception as exc:  # noqa: BLE001
        log.warning("xbrl %s failed: %s", sym, exc)
        return 0
    gaap = (data.get("facts") or {}).get("us-gaap") or (data.get("facts") or {}).get("ifrs-full") or {}
    dei = (data.get("facts") or {}).get("dei") or {}
    series = {k: _quarters(gaap, v) for k, v in FLOW.items()}
    cash = _instant(gaap, INSTANT["cash"])
    shares_by_accn = _shares(dei)
    ends = sorted({e for s in series.values() for e in s} , reverse=True)[:12]
    prev_latest = db.query_one("SELECT MAX(period_end) m FROM fundamentals WHERE symbol = ?", (sym,))["m"]
    for end in ends:
        ref = next((s[end][1] for s in series.values() if end in s), {})
        accn = next((s[end][2] for s in series.values() if end in s), ref.get("accn"))
        vals = {k: (s[end][0] if end in s else None) for k, s in series.items()}
        db.execute(
            """INSERT INTO fundamentals(symbol, period_end, fy, fp, form, accession, filed, revenue, gross_profit, op_income,
                                        net_income, eps_diluted, op_cash_flow, cash, shares_out)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, period_end) DO UPDATE SET revenue=excluded.revenue, gross_profit=excluded.gross_profit,
                 op_income=excluded.op_income, net_income=excluded.net_income, eps_diluted=excluded.eps_diluted,
                 cash=excluded.cash, shares_out=COALESCE(excluded.shares_out, fundamentals.shares_out),
                 filed=excluded.filed, accession=excluded.accession, form=excluded.form""",
            (sym, end, ref.get("fy"), ref.get("fp"), ref.get("form"), accn, ref.get("filed"), vals["revenue"],
             vals["gross_profit"], vals["op_income"], vals["net_income"], vals["eps_diluted"], None, cash.get(end),
             shares_by_accn.get(accn) or None),
        )
    latest = ends[0] if ends else None
    if latest and prev_latest and latest > prev_latest:
        _new_quarter_event(sym, latest)
    return len(ends)


def _fmt_usd(v):
    if v is None:
        return "—"
    a = abs(v)
    return f"{v/1e9:,.2f}B$" if a >= 1e9 else f"{v/1e6:,.1f}M$" if a >= 1e6 else f"{v:,.0f}$"


def _pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


def quarter_table(sym: str, n: int = 8) -> list[dict]:
    rows = [dict(r) for r in db.query("SELECT * FROM fundamentals WHERE symbol = ? ORDER BY period_end DESC LIMIT ?", (sym, n + 4))]
    for i, r in enumerate(rows):
        yo = next((x for x in rows[i + 1:] if 350 <= _days(x["period_end"], r["period_end"]) <= 380), None)
        qo = rows[i + 1] if i + 1 < len(rows) else None
        r["rev_yoy"] = _pct(r["revenue"], yo["revenue"] if yo else None)
        r["gross_margin"] = (r["gross_profit"] / r["revenue"] * 100) if r["gross_profit"] is not None and r["revenue"] else None
        r["shares_qoq"] = _pct(r["shares_out"], qo["shares_out"] if qo else None)
    return rows[:n]


def _new_quarter_event(sym: str, end: str) -> None:
    t = quarter_table(sym, 2)
    if not t:
        return
    r = t[0]
    parts = [f"gelir {_fmt_usd(r['revenue'])}" + (f" ({r['rev_yoy']:+.0f}% YoY)" if r["rev_yoy"] is not None else ""),
             f"net kâr {_fmt_usd(r['net_income'])}"]
    if r["eps_diluted"] is not None:
        parts.append(f"EPS {r['eps_diluted']:.2f}")
    if r["gross_margin"] is not None:
        parts.append(f"brüt marj %{r['gross_margin']:.1f}")
    if r["shares_qoq"] is not None:
        parts.append(f"hisse sayısı {r['shares_qoq']:+.1f}% QoQ")
    imp = 4 if (r["shares_qoq"] or 0) >= 5 else 3
    db.insert_event(
        type="filing", source="SEC XBRL", symbol=sym, title=f"{sym}: çeyrek finansalları ({end}) — " + ", ".join(parts),
        body=f"Resmi XBRL verisi. Son çeyrekler: {t}", url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={sym}&type=10-Q",
        published_at=iso(utcnow()), dedupe_key=sha("xbrl", sym, end), status="pending", importance=imp,
        extra={"period_end": end, "revenue": r["revenue"], "rev_yoy": r["rev_yoy"], "net_income": r["net_income"],
               "eps_diluted": r["eps_diluted"], "shares_qoq": r["shares_qoq"]},
    )


def refresh() -> int:
    return sum(refresh_ticker(t) for t in db.active_tickers())
