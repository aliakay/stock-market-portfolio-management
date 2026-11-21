"""Stock hub: one page per symbol, built in sections.

Every section is computed on the server from cached FMP / Yahoo / SEC data and
returns: a one-line result ("Sonuç"), a plain-language explanation ("Daha basit
anlat"), its numbers, and the sources with fetch times. Missing inputs are
listed as "doğrulanamadı" instead of being guessed.
"""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import config, db, options, portfolio, stockdata as sd, technicals
from .util import utcnow

log = logging.getLogger(__name__)


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def big(v) -> str:
    if not _isnum(v):
        return "—"
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:,.2f}{suf}$".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{v:,.0f}$"


def num(v, digits: int = 2) -> str:
    """Turkish number format: 1.234,56"""
    if not _isnum(v):
        return "—"
    return f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v, digits: int = 1, sign: bool = True) -> str:
    if not _isnum(v):
        return "—"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return "%" + s.replace(".", ",")


def _tone(v, good: float = 0, bad: float = 0) -> str:
    if v is None:
        return "neu"
    return "pos" if v > good else "neg" if v < bad else "neu"


def section(key, title, result, tone="neu", simple=None, data=None, sources=(), missing=()) -> dict:
    return {"key": key, "title": title, "result": result, "tone": tone, "simple": simple, "data": data or {},
            "sources": [s for s in sources if s], "missing": list(missing)}


# ── raw inputs ────────────────────────────────────────────

# TTLs in hours, sized for the free FMP plan (small daily quota): slow-moving data is kept for days so the nightly
# scorecards mostly read the cache; every section shows its fetch time, so the age is visible on the page.
FMP_CALLS = {
    "quote": ("quote", 1, {}), "profile": ("profile", 720, {}), "ptc": ("price-target-consensus", 24, {}),
    "pts": ("price-target-summary", 24, {}), "gc": ("grades-consensus", 24, {}), "ratios": ("ratios-ttm", 72, {}),
    "km": ("key-metrics-ttm", 72, {}), "scores": ("financial-scores", 168, {}), "float": ("shares-float", 168, {}),
}
FMP_LISTS = {
    "grades": ("grades", 24, {}), "est": ("analyst-estimates", 72, {"period": "annual", "limit": 6}),
    "inc_q": ("income-statement", 72, {"period": "quarter", "limit": 5}),
    "inc_a": ("income-statement", 336, {"period": "annual", "limit": 5}),
    "cf_q": ("cash-flow-statement", 72, {"period": "quarter", "limit": 5}),
    "cf_a": ("cash-flow-statement", 336, {"period": "annual", "limit": 5}),
    "bal_q": ("balance-sheet-statement", 72, {"period": "quarter", "limit": 1}),
    "ratios_a": ("ratios", 336, {"period": "annual", "limit": 5}),
    "seg": ("revenue-product-segmentation", 720, {"period": "annual", "structure": "flat"}),
    "geo": ("revenue-geographic-segmentation", 720, {"period": "annual", "structure": "flat"}),
    "execs": ("key-executives", 720, {}), "earn": ("earnings", 24, {"limit": 5}), "peers": ("stock-peers", 720, {}),
}
YAHOO_KINDS = ("eps_trend", "earnings_estimate", "revenue_estimate", "earnings_history", "growth_estimates",
               "insider_transactions", "info")


class Inputs(dict):
    """name → Fetched; `.d(name)` gives the data or a default."""

    def d(self, name, default=None):
        f = self.get(name)
        return f.data if f and f.data not in (None, [], {}) else default

    def stamp(self, name, label=None):
        return sd.stamp(self.get(name), label)


def fetch(symbol: str, with_options: bool = True) -> Inputs:
    symbol = symbol.upper()
    out = Inputs()
    jobs = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for name, (path, ttl, kw) in FMP_CALLS.items():
            jobs[name] = ex.submit(sd.fmp_one, path, ttl, symbol=symbol, **kw)
        for name, (path, ttl, kw) in FMP_LISTS.items():
            jobs[name] = ex.submit(sd.fmp, path, ttl, symbol=symbol, **kw)
        for k in YAHOO_KINDS:
            jobs["y_" + k] = ex.submit(sd.yahoo, k, symbol)
        jobs["tech"] = ex.submit(technicals.snapshot, symbol)
        if with_options:
            jobs["opt"] = ex.submit(options.analyze, symbol)
    for name, fut in jobs.items():
        try:
            out[name] = fut.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("hub fetch %s %s: %s", symbol, name, exc)
            out[name] = None
    return out


def _period(rows: list[dict] | None, period: str) -> dict:
    return next((r for r in rows or [] if r.get("period") == period), {})


def two_q_eps(trend: list[dict] | None, col: str = "current") -> float | None:
    q0, q1 = _period(trend, "0q").get(col), _period(trend, "+1q").get(col)
    return q0 + q1 if q0 is not None and q1 is not None else None


def price_on(symbol: str, day: str) -> tuple[str, float] | None:
    r = db.query_one("SELECT day, close FROM prices_daily WHERE symbol = ? AND day <= ? AND close IS NOT NULL "
                     "ORDER BY day DESC LIMIT 1", (symbol.upper(), day))
    return (r["day"], r["close"]) if r else None


# ── sections ──────────────────────────────────────────────

def numbers(sym: str, x: Inputs) -> dict:
    q, prof, r, km, info = x.d("quote", {}), x.d("profile", {}), x.d("ratios", {}), x.d("km", {}), x.d("y_info", {})
    t = x.get("tech") or {}
    price = q.get("price") or t.get("price")
    fpe = None
    eps2 = two_q_eps(x.d("y_eps_trend"))
    if price and eps2 and eps2 > 0:
        fpe = price / (eps2 * 2)
    rows = [
        ("Piyasa değeri", big(q.get("marketCap")), x.stamp("quote")),
        ("F/K (son 12 ay)", num(r['priceToEarningsRatioTTM'], 1) if r.get("priceToEarningsRatioTTM") else "doğrulanamadı", x.stamp("ratios")),
        ("İleri F/K (2 çeyrek tahmin × 2)", num(fpe, 1) if fpe else "doğrulanamadı", x.stamp("y_eps_trend", "Yahoo tahmin")),
        ("PEG (ileri)", num(r['forwardPriceToEarningsGrowthRatioTTM'], 2) if r.get("forwardPriceToEarningsGrowthRatioTTM") else "—", x.stamp("ratios")),
        ("FD/FAVÖK", num(km['evToEBITDATTM'], 1) if km.get("evToEBITDATTM") else "—", x.stamp("km")),
        ("Fiyat/Satış", num(r['priceToSalesRatioTTM'], 1) if r.get("priceToSalesRatioTTM") else "—", x.stamp("ratios")),
        ("Brüt marj", pct((r.get("grossProfitMarginTTM") or 0) * 100, sign=False) if r.get("grossProfitMarginTTM") is not None else "—", x.stamp("ratios")),
        ("Net marj", pct((r.get("netProfitMarginTTM") or 0) * 100, sign=False) if r.get("netProfitMarginTTM") is not None else "—", x.stamp("ratios")),
        ("Özsermaye kârlılığı", pct(km["returnOnEquityTTM"] * 100, sign=False) if km.get("returnOnEquityTTM") is not None else "—", x.stamp("km")),
        ("Borç/Özsermaye", num(r['debtToEquityRatioTTM'], 2) if r.get("debtToEquityRatioTTM") is not None else "—", x.stamp("ratios")),
        ("Serbest nakit getirisi", pct(km["freeCashFlowYieldTTM"] * 100) if km.get("freeCashFlowYieldTTM") is not None else "—", x.stamp("km")),
        ("Beta", num(prof['beta'], 2) if prof.get("beta") else "—", x.stamp("profile")),
        ("Açığa satış / halka açık", pct(info["shortPercentOfFloat"] * 100, sign=False) if info.get("shortPercentOfFloat") else "—", x.stamp("y_info")),
        ("Kurumsal sahiplik", pct(info["heldPercentInstitutions"] * 100, sign=False) if info.get("heldPercentInstitutions") else "—", x.stamp("y_info")),
    ]
    if t:
        rows += [
            ("52h aralık", f"{num(t['lo52'])} – {num(t['hi52'])} (zirveden {pct(t['from_hi52'])})", t["source"]),
            ("RSI(14) · ATR%", f"{num(t['rsi14'], 0)} · {pct(t['atr_pct'], sign=False)}" if t.get("rsi14") else "—", t["source"]),
        ]
    mine = [p for p in portfolio.positions() if p["symbol"] == sym]
    desc = prof.get("description") or info.get("longBusinessSummary") or ""
    result = f"{prof.get('companyName') or info.get('longName') or sym}: {prof.get('sector') or info.get('sector') or '—'} / {prof.get('industry') or info.get('industry') or '—'}."
    if mine:
        m = mine[0]
        result += f" Portföyünde {num(m['qty'], 4).rstrip('0').rstrip(',')} adet, ortalama {num(m['avg'])}$, {pct(m['unreal_pct'])}."
    return section("sayilar", "Sayılar", result, "neu",
                   simple="Şirketin büyüklüğü, ne kadar kâr ettiği, borcu ve hisse fiyatının kazancına göre pahalı mı ucuz mu olduğunu gösteren temel ölçüler.",
                   data={"rows": rows, "mine": mine, "desc": desc[:900], "fpe": fpe, "price": price,
                         "employees": info.get("fullTimeEmployees"), "website": prof.get("website")},
                   sources=[x.stamp("quote"), x.stamp("profile"), x.stamp("ratios")],
                   missing=[] if fpe else ["İleri F/K"])


def why(sym: str, x: Inputs, t: dict | None) -> dict:
    """Price move ≈ (1 + EPS change) × (1 + multiple change) − 1, over 30 and 90 days."""
    trend = x.d("y_eps_trend")
    price = (x.d("quote", {}) or {}).get("price") or (t or {}).get("price")
    rows, missing = [], []
    for days, col in ((30, "30daysAgo"), (90, "90daysAgo")):
        e_now, e_then = two_q_eps(trend), two_q_eps(trend, col)
        then = price_on(sym, (date.today() - timedelta(days=days)).isoformat())
        if not (price and e_now and e_then and then and e_now > 0 and e_then > 0):
            missing.append(f"{days} gün ayrıştırması")
            continue
        p_chg = (price / then[1] - 1) * 100
        eps_chg = (e_now / e_then - 1) * 100
        pe_now, pe_then = price / (e_now * 2), then[1] / (e_then * 2)
        pe_chg = (pe_now / pe_then - 1) * 100
        rows.append({"days": days, "from_day": then[0], "p_then": then[1], "p_now": price, "p_chg": p_chg,
                     "eps_then": e_then, "eps_now": e_now, "eps_chg": eps_chg,
                     "pe_then": pe_then, "pe_now": pe_now, "pe_chg": pe_chg})
    if not rows:
        return section("neden", "Neden?", "Fiyat hareketi ayrıştırılamadı: tahmin geçmişi doğrulanamadı.", "neu",
                       sources=[x.stamp("y_eps_trend", "Yahoo tahmin")], missing=missing)
    r = rows[0]
    if abs(r["p_chg"]) < 1:
        driver = "fiyat neredeyse yatay"
    elif abs(r["eps_chg"]) >= abs(r["pe_chg"]):
        driver = "hareketin çoğu kazanç beklentisindeki değişimden"
    else:
        driver = "hareketin çoğu çarpan (piyasanın ödemeye razı olduğu F/K) değişiminden"
    result = (f"Son 30 günde fiyat {pct(r['p_chg'])}; 2 çeyreklik EPS tahmini {pct(r['eps_chg'])}, "
              f"ileri F/K {num(r['pe_then'], 1)} → {num(r['pe_now'], 1)} ({pct(r['pe_chg'])}). Yani {driver}.")
    if r["eps_chg"] < -1 and r["p_chg"] > 1:
        result += " Tahminler düşerken fiyat yükseldi: hisse pahalanıyor."
    elif r["eps_chg"] > 1 and r["p_chg"] < -1:
        result += " Tahminler yükselirken fiyat düştü: hisse ucuzluyor."
    tone = "pos" if r["eps_chg"] > 0.5 else "neg" if r["eps_chg"] < -0.5 else "neu"
    return section("neden", "Neden?", result, tone,
                   simple="Hisse fiyatı iki şeyle değişir: şirketin kazanacağı tahmin edilen para (EPS) ve piyasanın o kazanca kaç kat ödemeye razı olduğu (F/K). "
                          "Burada son 30 ve 90 günde fiyat değişiminin ne kadarının hangisinden geldiğini ayırıyoruz. "
                          "EPS = bu çeyrek + gelecek çeyrek analist tahmini; ileri F/K = fiyat ÷ (bu toplam × 2).",
                   data={"rows": rows}, sources=[x.stamp("y_eps_trend", "Yahoo EPS tahmin geçmişi"), "Yahoo günlük kapanış"],
                   missing=missing)


def _growth(a, b):
    return (a / b - 1) * 100 if a is not None and b not in (None, 0) and b > 0 else None


def earnings(sym: str, x: Inputs) -> dict:
    inc_q, inc_a = x.d("inc_q", []), x.d("inc_a", [])
    cf_q, cf_a = {r["date"]: r for r in x.d("cf_q", [])}, {r["date"]: r for r in x.d("cf_a", [])}

    def table(rows, cfs):
        out = []
        for i, r in enumerate(rows):
            prev = rows[i + 4] if len(rows) > i + 4 and r.get("period", "").startswith("Q") else rows[i + 1] if len(rows) > i + 1 and r.get("period") == "FY" else None
            cf = cfs.get(r["date"], {})
            fcf = cf.get("freeCashFlow")
            out.append({"period": f"{r.get('fiscalYear')} {r.get('period')}", "date": r["date"], "revenue": r.get("revenue"),
                        "rev_g": _growth(r.get("revenue"), (prev or {}).get("revenue")),
                        "gross_m": r["grossProfit"] / r["revenue"] * 100 if r.get("revenue") and r.get("grossProfit") is not None else None,
                        "op_m": r["operatingIncome"] / r["revenue"] * 100 if r.get("revenue") and r.get("operatingIncome") is not None else None,
                        "net": r.get("netIncome"), "eps": r.get("epsDiluted"),
                        "eps_g": _growth(r.get("epsDiluted"), (prev or {}).get("epsDiluted")), "fcf": fcf})
        return out

    q_rows, a_rows = table(inc_q, cf_q), table(inc_a, cf_a)
    hist = []
    for h in x.d("y_earnings_history", []):
        hist.append({"quarter": (h.get("quarter") or "")[:10], "actual": h.get("epsActual"), "est": h.get("epsEstimate"),
                     "surprise": (h.get("surprisePercent") or 0) * 100 if h.get("surprisePercent") is not None else None})
    beats = sum(1 for h in hist if h["surprise"] is not None and h["surprise"] > 0)
    upcoming = sorted((e for e in x.d("earn", []) if e.get("epsActual") is None and e.get("date") >= date.today().isoformat()),
                      key=lambda e: e["date"])
    nxt = upcoming[0] if upcoming else None
    est = {p: _period(x.d("y_earnings_estimate"), p) for p in ("0q", "+1q", "0y", "+1y")}
    rev = {p: _period(x.d("y_revenue_estimate"), p) for p in ("0q", "+1q", "0y", "+1y")}

    def segs(name):
        rows = x.d(name, [])
        if not rows:
            return []
        cur = rows[0]
        prev = next((r for r in rows[1:] if r.get("fiscalYear") == (cur.get("fiscalYear") or 0) - 1), {})
        tot = sum(v for v in cur["data"].values() if v) or 1
        out = [{"name": k, "value": v, "share": v / tot * 100, "g": _growth(v, (prev.get("data") or {}).get(k)), "year": cur.get("fiscalYear")}
               for k, v in cur["data"].items() if v]
        return sorted(out, key=lambda s: -s["value"])

    last = q_rows[0] if q_rows else None
    parts, missing = [], []
    if last:
        parts.append(f"Son çeyrek ({last['period']}) gelir {big(last['revenue'])}, yıllık {pct(last['rev_g'])}; "
                     f"seyreltilmiş EPS {num(last['eps'])}$" + (f" ({pct(last['eps_g'])})" if last["eps_g"] is not None else ""))
    else:
        missing.append("Çeyreklik gelir tablosu")
    if hist:
        parts.append(f"son {len(hist)} çeyrekte tahmin aşımı: {beats}")
    if nxt:
        parts.append(f"sıradaki bilanço {nxt['date']}" + (f" (EPS tahmini {num(nxt['epsEstimated'])}$)" if nxt.get("epsEstimated") else ""))
    tone = _tone(last["rev_g"] if last else None, 5, 0)
    return section("kazanc", "Kazanç", "; ".join(parts) + "." if parts else "Kazanç verisi doğrulanamadı.", tone,
                   simple="Şirketin her çeyrekte ne kadar sattığı (gelir), bunun ne kadarının kâra dönüştüğü (marj) ve "
                          "analistlerin beklentisini tutturup tutturmadığı. Yıllık büyüme aynı çeyreğin bir yıl öncesiyle kıyaslanır.",
                   data={"quarters": q_rows, "annual": a_rows, "history": hist, "beats": beats, "next": nxt,
                         "est": est, "rev": rev, "segments": segs("seg"), "geo": segs("geo")},
                   sources=[x.stamp("inc_q", "FMP gelir tablosu"), x.stamp("cf_q", "FMP nakit akışı"),
                            x.stamp("y_earnings_history", "Yahoo sürpriz geçmişi"), x.stamp("earn", "FMP bilanço takvimi")],
                   missing=missing)


def experts(sym: str, x: Inputs) -> dict:
    ptc, pts, gc = x.d("ptc", {}), x.d("pts", {}), x.d("gc", {})
    price = (x.d("quote", {}) or {}).get("price")
    since = (date.today() - timedelta(days=90)).isoformat()
    grades = [g for g in x.d("grades", []) if g.get("date", "") >= since]
    by_day: dict[str, list] = {}
    for g in grades:
        by_day.setdefault(g["date"], []).append(g)
    ups = sum(1 for g in grades if g.get("action") == "upgrade")
    downs = sum(1 for g in grades if g.get("action") == "downgrade")
    est = sorted(x.d("est", []), key=lambda e: e["date"])
    this_year = date.today().year
    est_rows = [{"year": e["date"][:4], "rev": e.get("revenueAvg"), "eps": e.get("epsAvg"), "eps_lo": e.get("epsLow"),
                 "eps_hi": e.get("epsHigh"), "n": e.get("numAnalystsEps")} for e in est if int(e["date"][:4]) >= this_year - 1]
    for i, r in enumerate(est_rows):
        prev = est_rows[i - 1] if i else None
        r["eps_g"] = _growth(r["eps"], prev["eps"]) if prev else None
        r["rev_g"] = _growth(r["rev"], prev["rev"]) if prev else None
    rev_rows = []
    for p in x.d("y_eps_trend", []):
        rev_rows.append({"period": p["period"], "now": p.get("current"), "d7": p.get("7daysAgo"), "d30": p.get("30daysAgo"),
                         "d90": p.get("90daysAgo"), "chg30": _growth(p.get("current"), p.get("30daysAgo"))})
    total = sum(gc.get(k, 0) or 0 for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    buy_share = ((gc.get("strongBuy") or 0) + (gc.get("buy") or 0)) / total * 100 if total else None
    upside = (ptc["targetMedian"] / price - 1) * 100 if ptc.get("targetMedian") and price else None
    parts = []
    if ptc.get("targetMedian"):
        parts.append(f"Medyan hedef {num(ptc['targetMedian'])}$ ({pct(upside)} potansiyel), aralık {num(ptc.get('targetLow'))}–{num(ptc.get('targetHigh'))}$")
    if total:
        parts.append(f"{total} kurumda AL oranı %{buy_share:.0f}")
    if grades:
        parts.append(f"son 90 günde {ups} yükseltme, {downs} indirme")
    if pts.get("lastMonthAvgPriceTarget") and pts.get("lastQuarterAvgPriceTarget"):
        d = _growth(pts["lastMonthAvgPriceTarget"], pts["lastQuarterAvgPriceTarget"])
        parts.append(f"son ay hedef ortalaması çeyrek ortalamasına göre {pct(d)}")
    tone = _tone(upside, 10, 0)
    return section("uzmanlar", "Uzmanlar", "; ".join(parts) + "." if parts else "Analist verisi doğrulanamadı.", tone,
                   simple="Hisseyi takip eden bankaların analistleri ne diyor: 12 aylık hedef fiyatları, al/tut/sat dağılımı, "
                          "not değişiklikleri ve kazanç tahminlerinin son haftalarda yukarı mı aşağı mı revize edildiği.",
                   data={"ptc": ptc, "pts": pts, "gc": gc, "total": total, "buy_share": buy_share, "upside": upside, "ups": ups, "downs": downs,
                         "grades": sorted(by_day.items(), reverse=True)[:20], "est": est_rows, "revisions": rev_rows},
                   sources=[x.stamp("ptc", "FMP hedef fiyat"), x.stamp("gc", "FMP not dağılımı"), x.stamp("grades", "FMP not değişiklikleri"),
                            x.stamp("est", "FMP yıllık tahmin"), x.stamp("y_eps_trend", "Yahoo revizyon")])


def insiders(sym: str, x: Inputs) -> dict:
    since = (date.today() - timedelta(days=365)).isoformat()
    tx = []
    for r in x.d("y_insider_transactions", []):
        d = (r.get("Start Date") or "")[:10]
        if d < since:
            continue
        text = (r.get("Text") or "").lower()
        kind = "sat" if "sale" in text else "al" if "purchase" in text or "buy" in text else "hibe" if "gift" in text else \
            "opsiyon" if "option" in text or "exercise" in text else "ödül" if "award" in text or "grant" in text else "diğer"
        tx.append({"date": d, "who": (r.get("Insider") or "").title(), "role": r.get("Position"), "kind": kind,
                   "shares": r.get("Shares"), "value": r.get("Value"), "text": r.get("Text")})
    sells = sum(t["value"] or 0 for t in tx if t["kind"] == "sat")
    buys = sum(t["value"] or 0 for t in tx if t["kind"] == "al")
    n_buyers = len({t["who"] for t in tx if t["kind"] == "al"})
    q90 = (date.today() - timedelta(days=90)).isoformat()
    sells90 = sum(t["value"] or 0 for t in tx if t["kind"] == "sat" and t["date"] >= q90)
    form4 = [dict(r) for r in db.query(
        "SELECT id, title, published_at, direction, importance FROM events WHERE symbol = ? AND type = 'filing' "
        "AND title LIKE '%içeriden%' ORDER BY published_at DESC LIMIT 15", (sym,))]
    mcap = (x.d("quote", {}) or {}).get("marketCap")
    if buys > 0:
        result = f"Son 12 ayda {n_buyers} yönetici piyasadan {big(buys)} alım yaptı; satışlar {big(sells)}."
        tone = "pos"
    elif sells:
        share = sells / mcap * 100 if mcap else None
        result = f"Son 12 ayda piyasadan alım yok; satışlar {big(sells)} (son 90 gün {big(sells90)})" + \
                 (f"; piyasa değerine oranı %{num(share, 3)}" if share is not None else "") + "."
        tone = "neg" if share and share > 0.5 else "neu"
    else:
        result, tone = "Son 12 ayda kayda değer yönetici işlemi görünmüyor.", "neu"
    return section("yoneticiler", "Yöneticiler", result, tone,
                   simple="Şirket yöneticilerinin kendi hisselerini alıp satması. Piyasadan kendi parasıyla alım güçlü bir sinyaldir; "
                          "satışlar çoğu zaman planlı (vergi, çeşitlendirme) olduğundan tek başına olumsuz sayılmaz, ama yoğunlaşırsa dikkat.",
                   data={"tx": tx[:40], "sells": sells, "buys": buys, "sells90": sells90, "execs": x.d("execs", [])[:10], "form4": form4},
                   sources=[x.stamp("y_insider_transactions", "Yahoo içeriden işlemler"), x.stamp("execs", "FMP yöneticiler"), "SEC Form 4 (radar)"])


def big_money(sym: str, x: Inputs, t: dict | None) -> dict:
    from .collectors import positioning
    o = x.get("opt")
    si = positioning.short_interest_series(sym, 6)
    info = x.d("y_info", {})
    parts, missing = [], []
    if o:
        parts.append(f"Call duvarı {num(o['call_wall'], 1)}$, put duvarı {num(o['put_wall'], 1)}$, max pain {num(o['max_pain'], 1)}$ ({o['max_pain_expiry']})"
                     if o["call_wall"] and o["put_wall"] and o["max_pain"] else "Opsiyon duvarları belirsiz")
        parts.append(f"gamma {o['gamma_sign']}" + (f", dönüş seviyesi ≈{num(o['gamma_flip'], 0)}$" if o["gamma_flip"] else ""))
        if o["exp_move_pct"]:
            parts.append(f"{o['exp_move_expiry']} vadesine kadar beklenen hareket ±{num(o['exp_move'])}$ (%{num(o['exp_move_pct'], 1)})")
    else:
        missing.append("Opsiyon zinciri")
    if len(si) > 1 and si[0].get("short_qty") and si[-1].get("short_qty"):
        a, b = si[-1], si[0]
        parts.append(f"açığa satış miktarı {pct(_growth(a['short_qty'], b['short_qty']))} ({b['settlement_date']} → {a['settlement_date']}), "
                     f"kapatma {num(a['days_to_cover'] or 0, 1)} gün")
    pos = None
    if o and o["put_wall"] and o["call_wall"]:
        rng = o["call_wall"] - o["put_wall"]
        pos = (o["spot"] - o["put_wall"]) / rng * 100 if rng > 0 else None
    tone = "neu"
    if o:
        tone = "pos" if o["gex"] > 0 and (o["pc_oi"] or 1) < 0.9 else "neg" if o["gex"] < 0 and (o["pc_oi"] or 0) > 1.1 else "neu"
    return section("buyukpara", "Büyük para", "; ".join(p for p in parts if p) + "." if parts else "Opsiyon/akış verisi doğrulanamadı.", tone,
                   simple="Opsiyon piyasasında en çok pozisyon biriken fiyatlar (duvarlar) çoğu zaman mıknatıs/engel gibi çalışır. "
                          "Pozitif gamma: piyasa yapıcılar hareketi frenler (daha sakin); negatif gamma: hareketi büyütür (daha oynak). "
                          "Max pain: vade sonunda opsiyon alıcılarının en çok kaybettiği fiyat. Bu veriler ücretsiz kaynaktan, gecikmeli ve yaklaşıktır.",
                   data={"opt": o, "si": si, "inst": info.get("heldPercentInstitutions"), "ins": info.get("heldPercentInsiders"),
                         "short_float": info.get("shortPercentOfFloat"), "range_pos": pos, "hv20": (t or {}).get("hv20")},
                   sources=[o["stamp"] if o else None, "FINRA açığa satış (radar)", x.stamp("y_info", "Yahoo sahiplik")], missing=missing)


def analyses(sym: str) -> dict:
    rows = [dict(r) for r in db.query(
        """SELECT e.id, e.type, e.title, e.published_at, e.importance, COALESCE(a.direction, e.direction) AS direction,
                  a.summary_tr FROM events e LEFT JOIN analyses a ON a.event_id = e.id
           WHERE e.symbol = ? AND e.published_at >= ? AND COALESCE(a.importance, e.importance, 0) >= 3
                 AND e.status NOT IN ('duplicate', 'irrelevant')
           ORDER BY e.published_at DESC LIMIT 25""",
        (sym, (utcnow() - timedelta(days=30)).isoformat()))]
    pos = sum(1 for r in rows if (r["direction"] or 0) > 0)
    neg = sum(1 for r in rows if (r["direction"] or 0) < 0)
    reports = []
    rdir = config.REPORTS_DIR / sym
    if rdir.exists():
        reports = [p.name for p in sorted(rdir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:10]]
    if rows:
        result = f"Son 30 günde {len(rows)} önemli gelişme: {pos} olumlu, {neg} olumsuz, {len(rows) - pos - neg} nötr."
        tone = "pos" if pos > neg + 1 else "neg" if neg > pos + 1 else "neu"
    else:
        result, tone = "Son 30 günde radarda önemli gelişme yok" + (" (hisse izleme listesinde değil)." if not db.get_ticker(sym) else "."), "neu"
    return section("analizler", "Analizler", result, tone,
                   simple="Radarın topladığı haber, SEC dosyası ve analist notlarından önem puanı 3 ve üstü olanlar; her birinin yönü yapay zekâ analizinden.",
                   data={"events": rows, "pos": pos, "neg": neg, "reports": reports}, sources=["Borsa Radar olay akışı"])


def build(symbol: str, with_options: bool = True) -> dict:
    sym = symbol.upper()
    x = fetch(sym, with_options)
    t = x.get("tech")
    q = x.d("quote", {}) or {}
    secs = [numbers(sym, x), why(sym, x, t), analyses(sym), earnings(sym, x), experts(sym, x), insiders(sym, x), big_money(sym, x, t)]
    return {"symbol": sym, "name": q.get("name") or (x.d("profile", {}) or {}).get("companyName") or sym,
            "quote": q, "tech": t, "sections": {s["key"]: s for s in secs}, "order": [s["key"] for s in secs],
            "as_of": (t or {}).get("as_of"), "built_at": datetime.now(config.LOCAL_TZ), "inputs": x,
            "watched": bool(db.get_ticker(sym))}
