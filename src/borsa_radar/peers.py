"""Peer valuation comparison and peer suggestions.

`compare(["NVDA", "AMD", "AVGO"])` fetches the same twelve metrics for every
symbol (FMP stable TTM endpoints first, Yahoo statements as fallback), then
compares the first symbol with the median of the others and labels it
"ucuz / makul / pahalı (akranlara göre)" by a fixed, printed rule.

Every metric carries its source and fetch date. Missing numbers stay `None`
and are reported as "doğrulanamadı"; nothing is estimated. Multiples that are
not meaningful (negative earnings / EBITDA / FCF) are kept for display but
excluded from medians and from the label.

`suggest("SOFI")` proposes peers: FMP stock-peers → same industry inside the
radar universe (holdings, watchlist, setup pool; cached profiles only) →
a small static map.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
import time

from . import config, db, stockdata as sd
from .util import parse_dt, utcnow

log = logging.getLogger(__name__)

NA = "doğrulanamadı"
FMP_TTL = 24          # free plan: keep FMP calls rare (cache shared with the stock hub)
YAHOO_TTL = 12

# key, Turkish label, kind, format
#   kind: "size" (not compared), "val" (lower = cheaper), "qual" (higher = better), "lev" (lower = safer)
METRICS: list[tuple[str, str, str, str]] = [
    ("market_cap", "Piyasa değeri", "size", "big"),
    ("rev_growth", "Gelir büyümesi YoY (son çeyrek)", "qual", "pct"),
    ("gross_margin", "Brüt marj (TTM)", "qual", "pct"),
    ("op_margin", "Faaliyet marjı (TTM)", "qual", "pct"),
    ("net_margin", "Net marj (TTM)", "qual", "pct"),
    ("pe_ttm", "F/K (TTM)", "val", "x"),
    ("fwd_pe", "İleri F/K", "val", "x"),
    ("ev_rev", "FD/Satış (EV/Revenue)", "val", "x"),
    ("ev_ebitda", "FD/FAVÖK (EV/EBITDA)", "val", "x"),
    ("p_fcf", "Fiyat/Serbest nakit akışı (P/FCF)", "val", "x"),
    ("fcf_margin", "SNA marjı (TTM)", "qual", "pct"),
    ("net_debt_ebitda", "Net borç/FAVÖK", "lev", "x"),
]
LABELS = {k: lbl for k, lbl, _, _ in METRICS}
KIND = {k: kind for k, _, kind, _ in METRICS}
VAL_KEYS = [k for k, _, kind, _ in METRICS if kind == "val"]
QUAL_KEYS = [k for k, _, kind, _ in METRICS if kind == "qual"]

CHEAP_AT, RICH_AT, MIN_MULTIPLES = -15.0, 15.0, 2
RULE = (f"Kural: ilk hissenin {len(VAL_KEYS)} çarpandaki (F/K, ileri F/K, FD/Satış, FD/FAVÖK, P/FCF) akran medyanına göre "
        f"prim/iskonto yüzdelerinin medyanı alınır; ≤ −%{abs(CHEAP_AT):.0f} → ucuz, ≥ +%{RICH_AT:.0f} → pahalı, arası → makul. "
        f"En az {MIN_MULTIPLES} geçerli çarpan gerekir (negatif/zarar çarpanları sayılmaz); yoksa {NA}.")
CAVEAT = ("Not: daha yüksek büyüme ve marj, daha yüksek çarpanı haklı çıkarabilir; tersine düşük çarpan, zayıf büyümenin "
          "fiyatı da olabilir. Bu bir yatırım tavsiyesi değildir.")


# ── pure helpers (unit tested) ────────────────────────────

def isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


def ratio(a, b) -> float | None:
    return a / b if isnum(a) and isnum(b) and b != 0 else None


def growth_pct(now, then) -> float | None:
    return (now / then - 1) * 100 if isnum(now) and isnum(then) and then > 0 else None


def meaningful(key: str, v) -> bool:
    """Usable for medians: valuation multiples must be positive."""
    if not isnum(v):
        return False
    return v > 0 if KIND.get(key) == "val" else True


def median(values) -> float | None:
    xs = [v for v in values if isnum(v)]
    return statistics.median(xs) if xs else None


def premium(x, med) -> float | None:
    """% above (+) / below (−) the median. Undefined for non-positive medians."""
    return (x / med - 1) * 100 if isnum(x) and isnum(med) and med > 0 else None


def percentile_rank(x, others) -> float | None:
    """Share of `others` below x (ties count half), 0–100."""
    xs = [v for v in others if isnum(v)]
    if not isnum(x) or not xs:
        return None
    below = sum(1 for v in xs if v < x) + 0.5 * sum(1 for v in xs if v == x)
    return below / len(xs) * 100


def signed_pct(v, digits: int = 1) -> str:
    """+%12,3 / −%12,3"""
    if not isnum(v):
        return "—"
    return ("+" if v >= 0 else "−") + "%" + f"{abs(v):.{digits}f}".replace(".", ",")


def growth_adjusted_pe(fwd_pe, growth) -> float | None:
    """Forward P/E ÷ growth in percentage points (PEG-like). Only for positive P/E and growth."""
    if not (isnum(fwd_pe) and isnum(growth)) or fwd_pe <= 0 or growth <= 0:
        return None
    return fwd_pe / growth


def valuation_label(premiums: dict[str, float | None]) -> dict:
    used = {k: p for k, p in premiums.items() if k in VAL_KEYS and isnum(p)}
    if len(used) < MIN_MULTIPLES:
        return {"label": NA, "score": None, "used": used,
                "why": f"yalnızca {len(used)} geçerli çarpan var (en az {MIN_MULTIPLES} gerekli)"}
    score = statistics.median(used.values())
    lab = "ucuz" if score <= CHEAP_AT else "pahalı" if score >= RICH_AT else "makul"
    return {"label": f"{lab} (akranlara göre)", "short": lab, "score": score, "used": used,
            "why": f"{len(used)} çarpanın akran medyanına göre prim/iskonto medyanı {signed_pct(score)}"}


def quality_note(label_short: str | None, qual_prem: dict[str, float | None]) -> str | None:
    """Explain whether quality (growth/margins vs peers) supports the premium or discount."""
    xs = [p for p in qual_prem.values() if isnum(p)]
    if not xs or not label_short:
        return None
    better = sum(1 for p in xs if p > 0)
    if label_short == "pahalı" and better * 2 > len(xs):
        return (f"Prim kısmen açıklanabilir: {len(xs)} kalite ölçüsünün {better} tanesinde (büyüme/marj) akran medyanının üzerinde.")
    if label_short == "pahalı":
        return f"Dikkat: prim var ama {len(xs)} kalite ölçüsünün yalnızca {better} tanesinde akranların üzerinde."
    if label_short == "ucuz" and better * 2 < len(xs):
        return (f"İskonto zayıf büyüme/marjı yansıtıyor olabilir (değer tuzağı riski): {len(xs)} kalite ölçüsünün "
                f"yalnızca {better} tanesinde akranların üzerinde.")
    if label_short == "ucuz":
        return f"Kalite ölçülerinin {better}/{len(xs)} tanesinde akranların üzerinde olmasına rağmen iskontolu."
    return f"Kalite ölçülerinin {better}/{len(xs)} tanesinde akran medyanının üzerinde."


def analyze(table: dict[str, dict[str, dict]]) -> dict:
    """`table`: symbol → metric → cell {"value": ...}. First symbol is the target, the rest are peers."""
    syms = list(table)
    target, peers = syms[0], syms[1:]
    out: dict = {"target": target, "peers": peers, "metrics": {}}
    prem_all: dict[str, float | None] = {}
    for key, label, kind, _ in METRICS:
        tv = (table[target].get(key) or {}).get("value")
        pv = [(table[p].get(key) or {}).get("value") for p in peers]
        usable = [v for v in pv if meaningful(key, v)]
        med = median(usable)
        t_ok = meaningful(key, tv)
        prem = premium(tv, med) if t_ok and kind in ("val", "qual") else None   # leverage near 0: compare the gap, not %
        if kind == "qual" and t_ok and isnum(med) and med <= 0:   # e.g. negative peer margins: use the gap in points
            prem = None
        pr = percentile_rank(tv, usable) if t_ok else None
        out["metrics"][key] = {"label": label, "kind": kind, "median": med, "n_peers": len(usable),
                               "target": tv, "target_usable": t_ok, "premium_pct": prem,
                               "diff_pts": (tv - med) if kind in ("qual", "lev") and t_ok and isnum(med) else None,
                               "percentile": pr}
        prem_all[key] = prem
    lab = valuation_label({k: prem_all[k] for k in VAL_KEYS})
    qual = {k: (out["metrics"][k]["diff_pts"]) for k in QUAL_KEYS}
    lab["quality_note"] = quality_note(lab.get("short"), qual)
    out["label"] = lab
    gape = {s: growth_adjusted_pe((table[s].get("fwd_pe") or {}).get("value"), (table[s].get("rev_growth") or {}).get("value"))
            for s in syms}
    g_med = median(gape[p] for p in peers)
    out["growth_adjusted_pe"] = {"by_symbol": gape, "peer_median": g_med, "target_premium_pct": premium(gape[target], g_med),
                                 "rule": "Büyümeye göre düzeltilmiş F/K = ileri F/K ÷ gelir büyümesi (yüzde puan, son çeyrek YoY). "
                                         "Düşük = büyümesine göre daha ucuz. Negatif büyüme/F/K'da hesaplanmaz."}
    out["rule"], out["caveat"] = RULE, CAVEAT
    return out


# ── fetching ──────────────────────────────────────────────

def _day(f) -> str | None:
    d = parse_dt(f.fetched_at) if f and f.fetched_at else None
    return d.astimezone(config.LOCAL_TZ).date().isoformat() if d else None


def cell(value, f=None, label: str | None = None, note: str | None = None) -> dict:
    if not isnum(value):
        return {"value": None, "source": None, "fetched": None, "note": note or NA}
    src = label or (f.source if f else None)
    if f is not None and f.stale:
        src = f"{src} (bayat)"
    return {"value": float(value), "source": src, "fetched": _day(f) if f is not None else None, "note": note}


def _yahoo_series(rows: list[dict] | None, name: str) -> list[float | None]:
    """Values of one line item, newest period first."""
    row = next((r for r in rows or [] if r.get("index") == name), None)
    if not row:
        return []
    cols = sorted((k for k in row if k != "index"), reverse=True)
    return [row[c] for c in cols]


def _ttm(series: list) -> float | None:
    s = series[:4]
    return sum(s) if len(s) == 4 and all(isnum(v) for v in s) else None


def _fmp_rev_growth(inc_q: list[dict] | None) -> float | None:
    rows = inc_q or []
    if len(rows) < 5:
        return None
    a, b = rows[0], rows[4]
    if a.get("period") != b.get("period") or str(a.get("fiscalYear")) == str(b.get("fiscalYear")):
        return None
    return growth_pct(a.get("revenue"), b.get("revenue"))


_throttle = {"until": 0.0}   # after an FMP 429: read the cache only for the next 10 minutes
_throttle_lock = threading.Lock()


def _fmp(path: str, ttl: float = FMP_TTL, one: bool = False, **params) -> sd.Fetched:
    """FMP via the shared cache; after a 429 fall back to whatever is cached (any age) instead of calling again."""
    if time.monotonic() < _throttle["until"]:
        key = "fmp:" + path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        row = db.query_one("SELECT fetched_at, body FROM api_cache WHERE key = ?", (key,))
        data = db.loads(row["body"]) if row else None
        stale = bool(row) and (utcnow() - parse_dt(row["fetched_at"])).total_seconds() > ttl * 3600
        f = sd.Fetched(data, row["fetched_at"] if row else None, "FMP", stale, None if row else "FMP 429 (istek sınırı)")
    else:
        f = sd.fmp(path, ttl, **params)
        if f.error and "429" in f.error:
            with _throttle_lock:
                _throttle["until"] = time.monotonic() + 600
    if one and isinstance(f.data, list):
        f = sd.Fetched(f.data[0] if f.data else None, f.fetched_at, f.source, f.stale, f.error)
    return f


def _src(f: sd.Fetched | None, label: str) -> str:
    if f is None:
        return f"{label}: {NA}"
    if not f.fetched_at:
        return f"{label}: alınamadı" + (f" ({f.error})" if f.error else "")
    s = f"{label} · {_day(f)}"
    if f.stale:
        s += " (bayat)"
    if not f:
        s += " — boş / plana dahil değil"
    return s


def metrics(symbol: str) -> dict:
    """symbol → {"name", "industry", "cells": metric → cell, "sources": [...]}"""
    sym = symbol.upper()
    prof = _fmp("profile", one=True, symbol=sym)
    rat = _fmp("ratios-ttm", one=True, symbol=sym)
    km = _fmp("key-metrics-ttm", one=True, symbol=sym)
    incq = _fmp("income-statement", symbol=sym, period="quarter", limit=5)
    yinfo = sd.yahoo("info", sym, YAHOO_TTL)
    p, r, k, yi = prof.data or {}, rat.data or {}, km.data or {}, yinfo.data or {}
    c: dict[str, dict] = {}

    mcap_fmp = k.get("marketCap") or p.get("marketCap")
    c["market_cap"] = cell(mcap_fmp, km if k.get("marketCap") else prof, "FMP key-metrics-ttm" if k.get("marketCap") else "FMP profile") \
        if isnum(mcap_fmp) else cell(yi.get("marketCap"), yinfo, "Yahoo info")
    c["rev_growth"] = cell(_fmp_rev_growth(incq.data), incq, "FMP income-statement (çeyrek)")
    for key, fk in (("gross_margin", "grossProfitMarginTTM"), ("op_margin", "operatingProfitMarginTTM"), ("net_margin", "netProfitMarginTTM")):
        v = r.get(fk)
        c[key] = cell(v * 100 if isnum(v) else None, rat, "FMP ratios-ttm")
    pe = r.get("priceToEarningsRatioTTM")
    c["pe_ttm"] = cell(pe, rat, "FMP ratios-ttm") if isnum(pe) and pe != 0 else cell(yi.get("trailingPE"), yinfo, "Yahoo info")
    c["fwd_pe"] = cell(yi.get("forwardPE"), yinfo, "Yahoo info (forwardPE)")
    c["ev_rev"] = cell(k.get("evToSalesTTM"), km, "FMP key-metrics-ttm")
    c["ev_ebitda"] = cell(k.get("evToEBITDATTM"), km, "FMP key-metrics-ttm")
    c["p_fcf"] = cell(r.get("priceToFreeCashFlowRatioTTM") or None, rat, "FMP ratios-ttm")
    fm = ratio(r.get("freeCashFlowPerShareTTM"), r.get("revenuePerShareTTM"))
    c["fcf_margin"] = cell(fm * 100 if fm is not None else None, rat, "FMP ratios-ttm (SNA/hisse ÷ gelir/hisse)")
    c["net_debt_ebitda"] = cell(k.get("netDebtToEBITDATTM"), km, "FMP key-metrics-ttm")

    if any(c[m]["value"] is None for m in LABELS if m != "fwd_pe"):
        _yahoo_fill(sym, c)
    yahoo_used = c.pop("_yahoo_used", [])
    _flag_meaningless(c)
    used = [(prof, "FMP profile"), (rat, "FMP ratios-ttm"), (km, "FMP key-metrics-ttm"),
            (incq, "FMP income-statement"), (yinfo, "Yahoo info")] + yahoo_used
    return {"symbol": sym, "name": p.get("companyName") or yi.get("longName") or yi.get("shortName"),
            "industry": p.get("industry") or yi.get("industry"), "sector": p.get("sector") or yi.get("sector"),
            "cells": c, "sources": [_src(f, lbl) for f, lbl in used],
            "fmp_missing": not (rat or km)}


def _yahoo_fill(sym: str, c: dict) -> None:
    """Fill gaps from Yahoo quarterly statements (sum of the last 4 quarters)."""
    inc = sd.yahoo("quarterly_income_stmt", sym, YAHOO_TTL)
    cf = sd.yahoo("quarterly_cashflow", sym, YAHOO_TTL)
    bal = sd.yahoo("quarterly_balance_sheet", sym, YAHOO_TTL)
    rev_s = _yahoo_series(inc.data, "Total Revenue")
    rev = _ttm(rev_s)
    gp, op, ni = (_ttm(_yahoo_series(inc.data, n)) for n in ("Gross Profit", "Operating Income", "Net Income"))
    ebitda = _ttm(_yahoo_series(inc.data, "EBITDA")) or _ttm(_yahoo_series(inc.data, "Normalized EBITDA"))
    fcf = _ttm(_yahoo_series(cf.data, "Free Cash Flow"))
    debt_s = _yahoo_series(bal.data, "Total Debt")
    cash_s = _yahoo_series(bal.data, "Cash Cash Equivalents And Short Term Investments") or _yahoo_series(bal.data, "Cash And Cash Equivalents")
    debt = debt_s[0] if debt_s and isnum(debt_s[0]) else (0.0 if debt_s == [] and bal.data else None)
    cash = cash_s[0] if cash_s and isnum(cash_s[0]) else None
    net_debt = debt - cash if isnum(debt) and isnum(cash) else None
    mcap = c["market_cap"]["value"]
    ev = mcap + net_debt if isnum(mcap) and isnum(net_debt) else None
    L = "Yahoo çeyreklik tablolar (son 4Ç)"
    c["_yahoo_used"] = [(inc, "Yahoo gelir tablosu"), (cf, "Yahoo nakit akışı"), (bal, "Yahoo bilanço")]

    def fill(key, value, f, label=L):
        if c[key]["value"] is None and isnum(value):
            c[key] = cell(value, f, label)

    fill("rev_growth", growth_pct(rev_s[0], rev_s[4]) if len(rev_s) >= 5 else None, inc, "Yahoo çeyreklik gelir (YoY)")
    for key, v in (("gross_margin", gp), ("op_margin", op), ("net_margin", ni)):
        fill(key, ratio(v, rev) * 100 if ratio(v, rev) is not None else None, inc)
    fill("pe_ttm", ratio(mcap, ni), inc, "Yahoo (piyasa değeri ÷ son 4Ç net kâr)")
    fill("ev_rev", ratio(ev, rev), bal, "Yahoo (FD = piyasa değeri + borç − nakit; son 4Ç)")
    fill("ev_ebitda", ratio(ev, ebitda), bal, "Yahoo (FD ÷ son 4Ç FAVÖK)")
    fill("p_fcf", ratio(mcap, fcf), cf, "Yahoo (piyasa değeri ÷ son 4Ç SNA)")
    fill("fcf_margin", ratio(fcf, rev) * 100 if ratio(fcf, rev) is not None else None, cf)
    fill("net_debt_ebitda", ratio(net_debt, ebitda) if isnum(ebitda) and ebitda > 0 else None, bal, "Yahoo (net borç ÷ son 4Ç FAVÖK)")


def _flag_meaningless(c: dict) -> None:
    for key in VAL_KEYS:
        v = c[key]["value"]
        if isnum(v) and v <= 0:
            c[key]["note"] = "anlamsız (negatif kazanç/FAVÖK/SNA) — medyana katılmaz"
    ev_ebitda = c["ev_ebitda"]["value"]
    if isnum(ev_ebitda) and ev_ebitda < 0 and c["net_debt_ebitda"]["value"] is not None:
        c["net_debt_ebitda"]["note"] = "FAVÖK negatif — oran anlamsız"
        c["net_debt_ebitda"]["value_raw"] = c["net_debt_ebitda"]["value"]
        c["net_debt_ebitda"]["value"] = None


def compare(symbols: list[str]) -> dict:
    syms = list(dict.fromkeys(s.upper() for s in symbols))
    if len(syms) < 2:
        raise ValueError("en az iki sembol gerekli (ilk sembol hedef, diğerleri akran)")
    rows = {s: metrics(s) for s in syms}   # sequential: the FMP free plan answers bursts with 429
    res = analyze({s: rows[s]["cells"] for s in syms})
    res["symbols"] = {s: {k: v for k, v in rows[s].items() if k != "cells"} for s in syms}
    res["table"] = {s: rows[s]["cells"] for s in syms}
    res["missing"] = {s: [LABELS[k] for k, cl in rows[s]["cells"].items() if cl["value"] is None] for s in syms}
    origin = {s: {("Yahoo" if (cl["source"] or "").startswith("Yahoo") else "FMP") for k, cl in rows[s]["cells"].items()
                  if cl["value"] is not None and k != "fwd_pe"} for s in syms}
    if len(set().union(*origin.values())) > 1:
        res["source_note"] = ("Kaynaklar karışık: " + ", ".join(f"{s} {'+'.join(sorted(o)) or '—'}" for s, o in origin.items())
                              + ". FMP TTM oranları ile Yahoo son-4-çeyrek hesapları tanım ve tarih olarak küçük farklar gösterebilir.")
    res["url"] = f"{config.PUBLIC_BASE_URL}/h/{syms[0]}"
    return res


# ── peer suggestions ──────────────────────────────────────

STATIC_GROUPS: dict[str, list[str]] = {
    "yarı iletken": ["NVDA", "AMD", "AVGO", "INTC", "QCOM", "MU", "MRVL", "TSM", "ARM", "TXN", "ADI", "ASML", "AMAT", "LRCX", "KLAC"],
    "mega-cap teknoloji": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "TSLA"],
    "fintek": ["SOFI", "AFRM", "UPST", "PYPL", "XYZ", "HOOD", "NU", "LC", "COIN"],
    "enerji depolama": ["EOSE", "FLNC", "ENVX", "QS", "GWH", "STEM", "BE"],
    "uzay": ["RKLB", "ASTS", "LUNR", "PL", "RDW", "BKSY", "IRDM", "SPCE"],
    "yapay zekâ bulutu": ["CRWV", "NBIS", "ORCL", "APLD", "IREN", "CIFR", "WULF", "CORZ"],
}


def static_peers(symbol: str) -> tuple[str | None, list[str]]:
    sym = symbol.upper()
    for group, members in STATIC_GROUPS.items():
        if sym in members:
            return group, [m for m in members if m != sym]
    if sym == "GOOG":
        return "mega-cap teknoloji", [m for m in STATIC_GROUPS["mega-cap teknoloji"] if m != "GOOGL"]
    return None, []


def _cached_profile(sym: str) -> dict:
    """Industry/sector/name from the cache only (no network)."""
    out: dict = {}
    for key in (f"fmp:profile?symbol={sym}", f"yahoo:info:{sym}"):
        row = db.query_one("SELECT body FROM api_cache WHERE key = ?", (key,))
        data = db.loads(row["body"]) if row else None
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            found = {"industry": data.get("industry"), "sector": data.get("sector"),
                     "name": data.get("companyName") or data.get("longName"), "market_cap": data.get("marketCap")}
            for k, v in found.items():
                if v and not out.get(k):
                    out[k] = v
    return out


def radar_universe() -> list[str]:
    syms: list[str] = []
    try:
        from . import setups
        syms = [u["symbol"] for u in setups.universe()]
    except Exception:  # noqa: BLE001 - fall back to watchlist + pool if the ledger fails
        log.exception("universe failed")
        syms = [t["symbol"] for t in db.active_tickers()] + (db.get_setting("setup_extra") or "").replace(",", " ").split()
    return list(dict.fromkeys(s.upper() for s in syms))


def universe_matches(industry: str | None, sector: str | None, exclude: str, universe: list[str],
                     profiles: dict[str, dict]) -> list[dict]:
    """Same industry first, then same sector (weaker)."""
    out = []
    for s in universe:
        if s == exclude:
            continue
        pr = profiles.get(s) or {}
        if industry and pr.get("industry") == industry:
            out.append({"symbol": s, "name": pr.get("name"), "why": f"aynı endüstri: {industry}", "strength": 2})
        elif sector and pr.get("sector") == sector:
            out.append({"symbol": s, "name": pr.get("name"), "why": f"aynı sektör (daha zayıf eşleşme): {sector}", "strength": 1})
    out.sort(key=lambda r: -r["strength"])
    return out


def suggest(symbol: str, limit: int = 8) -> dict:
    sym = symbol.upper()
    prof = _fmp("profile", one=True, symbol=sym)
    me = dict(_cached_profile(sym))
    if prof.data:
        me.update(industry=prof.data.get("industry"), sector=prof.data.get("sector"), name=prof.data.get("companyName"))
    if not me.get("industry"):
        yi = sd.yahoo("info", sym, YAHOO_TTL).data or {}
        me.update(industry=yi.get("industry"), sector=yi.get("sector"), name=me.get("name") or yi.get("longName"))

    uni = radar_universe()
    in_uni = universe_matches(me.get("industry"), me.get("sector"), sym, uni, {s: _cached_profile(s) for s in uni})

    peers, source, tried = [], None, []
    fp = _fmp("stock-peers", 72, symbol=sym)
    if fp.data:
        peers = [{"symbol": r["symbol"], "name": r.get("companyName"), "market_cap": r.get("mktCap"), "why": "FMP stock-peers"}
                 for r in fp.data if r.get("symbol") and r["symbol"] != sym]
        source = sd.stamp(fp, "FMP stock-peers")
    else:
        tried.append(f"FMP stock-peers: {fp.error or 'boş / plana dahil değil'}")
    strong = [r for r in in_uni if r["strength"] == 2]
    if not peers and strong:
        peers = [{k: v for k, v in r.items() if k != "strength"} for r in strong]
        source = "radar evreni (portföy + izleme + setup havuzu), önbellekteki profiller: aynı endüstri"
    elif not peers:
        tried.append("radar evreninde aynı endüstri yok (profili önbellekte olmayan semboller eşleşmez)")
    if not peers:
        group, members = static_peers(sym)
        if members:
            peers = [{"symbol": m, "name": None, "why": f"sabit liste: {group}"} for m in members]
            source = f"sabit liste ({group})"
        else:
            tried.append("sabit listede yok")
    if not peers and in_uni:   # last resort: same sector only
        peers = [{k: v for k, v in r.items() if k != "strength"} for r in in_uni]
        source = "radar evreni: yalnızca aynı sektör (zayıf eşleşme)"
    peers = peers[:limit]
    pick = [p["symbol"] for p in peers[:4]]
    return {"symbol": sym, "name": me.get("name"), "industry": me.get("industry"), "sector": me.get("sector"),
            "peers": peers, "source": source or NA, "tried": tried,
            "in_universe": [{k: v for k, v in r.items() if k != "strength"} for r in in_uni][:limit],
            "next": f"borsa-radar karsilastir {sym} {' '.join(pick)}" if pick else None}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
