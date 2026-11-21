"""Stock scorecard ("karne"): six dimensions scored 0-100 on the server.

The model only writes the one-sentence summary, and that sentence is rejected
if it contains a number that was not in the input. Missing dimensions are left
out and the remaining weights are renormalized. Every stored card keeps its
price so the forward returns per color band can be measured ("sicil").
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import date, datetime, timedelta

from . import config, db, hub, llm, options, prices
from .util import iso, utcnow, utcnow_iso

log = logging.getLogger(__name__)

WEIGHTS_VER = "v1"
PE_CAP = 40
DIMS = [  # key, name, default weight, plain explanation
    ("temel", "Temel Sağlamlık", 30, "Büyüme, kârlılık, borç ve finansal sağlık skorları (Piotroski, Altman Z)."),
    ("degerleme", "Değerleme", 20, "Üç yöntemle hesaplanan adil değere göre fiyatın ucuz/pahalı olması."),
    ("trend", "Trend / Teknik", 20, "Hareketli ortalamalar, 3 aylık momentum, S&P 500'e göre güç, RSI."),
    ("opsiyon", "Opsiyon & Akış", 15, "Put/Call oranları, gamma yönü, olağandışı opsiyon akışı (yaklaşık, ücretsiz veri)."),
    ("haber", "Haber & Analist Tonu", 10, "Radar gelişmelerinin yönü, not değişiklikleri, EPS revizyonları, hedef fiyat eğilimi."),
    ("anomali", "Anomali", 5, "Uyarı işaretleri: hisse sulandırma, yoğun içeriden satış, açığa satış artışı, aşırı RSI."),
]
BANDS = [(67, "pos", "Güçlü"), (45, "warn", "Karışık"), (0, "neg", "Zayıf")]


def clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def band(score: float | None) -> tuple[str, str]:
    if score is None:
        return "neu", "Yetersiz veri"
    for cut, tone, label in BANDS:
        if score >= cut:
            return tone, label
    return "neg", "Zayıf"


def _avg(parts: list[tuple]) -> float | None:
    vals = [p[2] for p in parts if p[2] is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _f(v, fmt="{:.2f}"):
    return "—" if v is None else fmt.format(v)


# ── fair value ────────────────────────────────────────────

def fair_value(x: hub.Inputs, price: float | None) -> dict:
    est = {p: hub._period(x.d("y_earnings_estimate"), p) for p in ("0y", "+1y")}
    eps_fwd = est["+1y"].get("avg") or est["0y"].get("avg")
    ge = {r.get("period"): r for r in x.d("y_growth_estimates", [])}
    g = next((ge[k]["stockTrend"] * 100 for k in ("LTG", "+1y") if ge.get(k, {}).get("stockTrend") is not None), None)
    if g is None and est["+1y"].get("growth") is not None:
        g = est["+1y"]["growth"] * 100
    pes = [r.get("priceToEarningsRatio") for r in x.d("ratios_a", [])]
    pes = [p for p in pes if p and 5 <= p <= 100]
    methods = []
    tgt = (x.d("ptc", {}) or {}).get("targetMedian")
    if tgt:
        methods.append({"name": "Analist medyan hedefi", "value": tgt, "how": "12 aylık hedef fiyatların medyanı", "src": x.stamp("ptc")})
    if eps_fwd and eps_fwd > 0 and len(pes) >= 3:
        mpe = statistics.median(pes)
        used = min(mpe, PE_CAP)
        note = f" (üst sınır {PE_CAP})" if mpe > PE_CAP else ""
        methods.append({"name": "Tarihsel F/K", "value": used * eps_fwd,
                        "how": f"son {len(pes)} yılın medyan F/K'sı {hub.num(mpe, 1)}{note} × gelecek yıl EPS tahmini {hub.num(eps_fwd)}",
                        "src": x.stamp("ratios_a")})
    if eps_fwd and eps_fwd > 0 and g is not None:
        gg = clamp(g, 0, 20)
        methods.append({"name": "Büyüme ayarlı F/K", "value": eps_fwd * (7 + 1.5 * gg),
                        "how": f"EPS {hub.num(eps_fwd)} × (7 + 1,5 × büyüme %{hub.num(gg, 1)}); Graham formülünün temkinli hali, büyüme 0–20 arası",
                        "src": x.stamp("y_growth_estimates", "Yahoo büyüme tahmini")})
    if not methods or not price:
        return {"methods": methods, "fair": None, "upside": None, "confidence": "yok", "eps_fwd": eps_fwd, "growth": g}
    vals = [m["value"] for m in methods]
    fair = statistics.median(vals)
    spread = (max(vals) - min(vals)) / fair if fair else 1
    conf = "düşük" if len(vals) == 1 or spread >= 0.45 else "orta" if spread >= 0.2 else "yüksek"
    return {"methods": methods, "fair": fair, "upside": (fair / price - 1) * 100, "confidence": conf, "spread": spread * 100,
            "eps_fwd": eps_fwd, "growth": g}


# ── dimensions ────────────────────────────────────────────

def dim_temel(x: hub.Inputs) -> tuple[float | None, list]:
    r, km, sc = x.d("ratios", {}), x.d("km", {}), x.d("scores", {})
    inc = x.d("inc_q", [])
    g = hub._growth(inc[0].get("revenue"), inc[4].get("revenue")) if len(inc) >= 5 else None
    nm = r.get("netProfitMarginTTM")
    roe = km.get("returnOnEquityTTM")
    de = r.get("debtToEquityRatioTTM")
    pio, z = sc.get("piotroskiScore"), sc.get("altmanZScore")
    fcfy = km.get("freeCashFlowYieldTTM")
    parts = [
        ("Gelir büyümesi (yıllık, son çeyrek)", hub.pct(g), clamp(40 + g * 2) if g is not None else None),
        ("Net marj", hub.pct(nm * 100, sign=False) if nm is not None else "—", clamp(30 + nm * 250) if nm is not None else None),
        ("Özsermaye kârlılığı", hub.pct(roe * 100, sign=False) if roe is not None else "—", clamp(30 + roe * 150) if roe is not None else None),
        ("Borç/Özsermaye", hub.num(de), clamp(95 - de * 25) if de is not None and de >= 0 else 30 if de is not None else None),
        ("Piotroski (0-9)", _f(pio, "{:.0f}"), pio / 9 * 100 if pio is not None else None),
        ("Altman Z", hub.num(z), clamp(z * 22) if z is not None else None),
        ("Serbest nakit getirisi", hub.pct(fcfy * 100) if fcfy is not None else "—", clamp(55 + fcfy * 800) if fcfy is not None else None),
    ]
    return _avg(parts), parts


def dim_degerleme(x: hub.Inputs, fv: dict) -> tuple[float | None, list]:
    if fv["fair"] is None:
        return None, [("Adil değer", "doğrulanamadı", None)]
    s = clamp(50 + fv["upside"] * 0.8)
    peg = (x.d("ratios", {}) or {}).get("forwardPriceToEarningsGrowthRatioTTM")
    parts = [("Adil değere uzaklık", hub.pct(fv["upside"]), s)]
    if peg is not None and peg > 0:
        parts.append(("İleri PEG", f"{peg:.2f}", clamp(90 - peg * 25)))
    score = _avg(parts)
    if fv["confidence"] == "düşük":
        score = round(50 + (score - 50) * 0.5, 1)
    return score, parts + [("Güven", fv["confidence"], None)]


def dim_trend(t: dict | None) -> tuple[float | None, list]:
    if not t:
        return None, []
    yn = lambda b: "üstünde" if b else "altında"  # noqa: E731
    parts = [
        ("Fiyat 20/50/200 günlük ortalamaya göre", f"{yn(t['above20'])} / {yn(t['above50'])} / {yn(t['above200'])}", None),
        ("3 aylık değişim", hub.pct(t["chg_63d"]), None),
        ("S&P 500'e göre (3 ay)", hub.pct(t["rs63_vs_spy"]), None),
        ("RSI(14)", _f(t["rsi14"], "{:.0f}"), None),
        ("Durum", t["trend_label"], None),
    ]
    return t["trend_score"], parts


def dim_opsiyon(o: dict | None, t: dict | None) -> tuple[float | None, list]:
    s, why = options.score(o, (t or {}).get("hv20"))
    return s, [(w, "", None) for w in why]


def dim_haber(sym: str, x: hub.Inputs) -> tuple[float | None, list]:
    since = iso(utcnow() - timedelta(days=14))
    ev = db.query("""SELECT COALESCE(a.direction, e.direction, 0) d, COALESCE(a.importance, e.importance, 1) i
                     FROM events e LEFT JOIN analyses a ON a.event_id = e.id
                     WHERE e.symbol = ? AND e.published_at >= ? AND e.type IN ('news','filing','rating')
                       AND e.status NOT IN ('duplicate','irrelevant','backfill')""", (sym, since))
    parts = []
    w = sum(r["i"] for r in ev)
    if w:
        net = sum(r["d"] * r["i"] for r in ev) / w
        parts.append((f"Radar gelişmeleri (14 gün, {len(ev)} adet)", f"net yön {net:+.2f}", clamp(50 + net * 40)))
    since30 = (date.today() - timedelta(days=30)).isoformat()
    gr = [g for g in x.d("grades", []) if g.get("date", "") >= since30]
    ups = sum(1 for g in gr if g.get("action") == "upgrade")
    downs = sum(1 for g in gr if g.get("action") == "downgrade")
    if ups + downs:
        parts.append(("Not değişiklikleri (30 gün)", f"{ups} yükseltme / {downs} indirme", clamp(50 + (ups - downs) / (ups + downs) * 35)))
    trend = x.d("y_eps_trend")
    e_now, e_30 = hub.two_q_eps(trend), hub.two_q_eps(trend, "30daysAgo")
    if e_now and e_30 and e_30 > 0:
        chg = (e_now / e_30 - 1) * 100
        parts.append(("2 çeyrek EPS revizyonu (30 gün)", hub.pct(chg), clamp(50 + chg * 6)))
    pts = x.d("pts", {}) or {}
    if pts.get("lastMonthAvgPriceTarget") and pts.get("lastQuarterAvgPriceTarget") and pts.get("lastMonthCount", 0) >= 2:
        g = hub._growth(pts["lastMonthAvgPriceTarget"], pts["lastQuarterAvgPriceTarget"])
        parts.append(("Hedef fiyat eğilimi (ay vs çeyrek)", hub.pct(g), clamp(50 + g * 4)))
    return _avg(parts), parts


def dim_anomali(sym: str, x: hub.Inputs, t: dict | None) -> tuple[float | None, list]:
    s, parts = 70.0, []
    ins = hub.insiders(sym, x)["data"]
    mcap = (x.d("quote", {}) or {}).get("marketCap")
    if ins["buys"] > 0:
        s += 20
        parts.append(("İçeriden piyasa alımı (12 ay)", hub.big(ins["buys"]), +20))
    if mcap and ins["sells90"] / mcap > 0.001:
        s -= 15
        parts.append(("Yoğun içeriden satış (90 gün)", hub.big(ins["sells90"]), -15))
    inc = x.d("inc_q", [])
    if len(inc) >= 5 and inc[0].get("weightedAverageShsOutDil") and inc[4].get("weightedAverageShsOutDil"):
        dil = hub._growth(inc[0]["weightedAverageShsOutDil"], inc[4]["weightedAverageShsOutDil"])
        if dil is not None and dil > 3:
            s -= 15
            parts.append(("Hisse sayısı artışı (sulandırma, yıllık)", hub.pct(dil), -15))
        elif dil is not None and dil < -1:
            s += 5
            parts.append(("Geri alım (hisse sayısı azalıyor)", hub.pct(dil), +5))
    from .collectors import positioning
    si = positioning.short_interest_series(sym, 3)
    if len(si) >= 2 and si[0]["short_qty"] and si[-1]["short_qty"]:
        ch = hub._growth(si[-1]["short_qty"], si[0]["short_qty"])
        if ch is not None and ch > 20:
            s -= 10
            parts.append(("Açığa satış artışı", hub.pct(ch), -10))
        elif ch is not None and ch < -20:
            s += 5
            parts.append(("Açığa satış azalışı", hub.pct(ch), +5))
    if t and t.get("rsi14") is not None and (t["rsi14"] > 80 or t["rsi14"] < 20):
        s -= 10
        parts.append(("Aşırı RSI", f"{t['rsi14']:.0f}", -10))
    if not parts:
        parts.append(("Belirgin uyarı işareti yok", "", 0))
    return round(clamp(s), 1), parts


# ── card ──────────────────────────────────────────────────

def compute(symbol: str, x: hub.Inputs | None = None) -> dict:
    sym = symbol.upper()
    x = x if x is not None else hub.fetch(sym)
    t = x.get("tech")
    o = x.get("opt")
    price = (t or {}).get("price") or (x.d("quote", {}) or {}).get("price")
    fv = fair_value(x, price)
    results = {
        "temel": dim_temel(x), "degerleme": dim_degerleme(x, fv), "trend": dim_trend(t),
        "opsiyon": dim_opsiyon(o, t), "haber": dim_haber(sym, x), "anomali": dim_anomali(sym, x, t),
    }
    dims = []
    for key, name, weight, expl in DIMS:
        score, parts = results[key]
        tone, label = band(score)
        dims.append({"key": key, "name": name, "weight": weight, "score": score, "tone": tone, "label": label, "explain": expl,
                     "parts": [{"label": p[0], "value": p[1], "pts": p[2]} for p in parts]})
    overall = weighted(dims, {d["key"]: d["weight"] for d in dims})
    tone, label = band(overall)
    return {"symbol": sym, "as_of": (t or {}).get("as_of"), "price": price, "overall": overall, "tone": tone, "label": label,
            "dims": dims, "fair": fv, "weights_ver": WEIGHTS_VER, "options_stamp": (o or {}).get("stamp"),
            "sources": [x.stamp("ratios", "FMP oranlar"), x.stamp("scores", "FMP skorlar"), x.stamp("ptc", "FMP hedef"),
                        x.stamp("y_eps_trend", "Yahoo tahmin"), (o or {}).get("stamp"), "Yahoo günlük kapanış " + ((t or {}).get("as_of") or "")]}


def weighted(dims: list[dict], weights: dict[str, float]) -> float | None:
    have = [(d["score"], weights.get(d["key"], 0)) for d in dims if d["score"] is not None and weights.get(d["key"], 0) > 0]
    tw = sum(w for _, w in have)
    return round(sum(s * w for s, w in have) / tw, 1) if tw else None


def template_summary(card: dict) -> str:
    scored = [d for d in card["dims"] if d["score"] is not None]
    if not scored or card["overall"] is None:
        return f"{card['symbol']} için karne hesaplanacak kadar veri doğrulanamadı."
    best = max(scored, key=lambda d: d["score"])
    worst = min(scored, key=lambda d: d["score"])
    s = (f"{card['symbol']} karnesi {card['overall']:.0f}/100 ({card['label'].lower()}); en güçlü yan {best['name'].lower()} "
         f"({best['score']:.0f}), en zayıf yan {worst['name'].lower()} ({worst['score']:.0f}).")
    fv = card["fair"]
    if fv.get("fair"):
        s += f" Adil değer tahmini {hub.num(fv['fair'])}$ ({hub.pct(fv['upside'])}), güven {fv['confidence']}."
    return s


NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in NUM_RE.findall(text):
        v = m.replace(",", ".")
        out.add(v.rstrip("0").rstrip(".") if "." in v else v)
    return out


def llm_summary(card: dict) -> tuple[str, str]:
    facts = {
        "sembol": card["symbol"], "genel_puan": round(card["overall"] or 0), "bant": card["label"],
        "boyutlar": [{"ad": d["name"], "puan": None if d["score"] is None else round(d["score"]),
                      "gerekceler": [f"{p['label']}: {p['value']}".strip(": ") for p in d["parts"]][:5]} for d in card["dims"]],
        "adil_deger": None if not card["fair"].get("fair") else round(card["fair"]["fair"], 2),
        "adil_deger_potansiyel_yuzde": None if card["fair"].get("upside") is None else round(card["fair"]["upside"], 1),
        "guven": card["fair"].get("confidence"),
    }
    payload = json.dumps(facts, ensure_ascii=False)
    prompt = (
        "Aşağıdaki hisse karnesini Türkçe, en fazla 2 kısa cümleyle (toplam 45 kelimeyi geçme) özetle. Bütün boyutları sayma; "
        "sonucu en çok belirleyen 2-3 noktayı seç. Kurallar: yalnızca verilen sayıları kullan, yeni sayı, "
        "tarih veya yüzde UYDURMA; al/sat tavsiyesi verme; 'Yani' ile başlayan sade bir yorum cümlesiyle bitir. "
        "Sadece özet metnini yaz, başka hiçbir şey yazma.\n\n" + payload
    )
    try:
        text, model = llm.complete(prompt, timeout=120, budget="daily")
    except llm.LLMError as exc:
        log.info("scorecard summary fallback: %s", exc)
        return template_summary(card), "şablon"
    text = " ".join(text.strip().strip('"').split())
    allowed = _numbers(payload) | {"100", "0", "12", "20", "50", "200", "30", "90", "14", "3", "5"}
    bad = _numbers(text) - allowed
    if bad or not text or len(text) > 600:
        log.warning("scorecard summary rejected (%s): %s", bad, text[:200])
        return template_summary(card), "şablon (model metni doğrulanamadı)"
    return text, model


def save(card: dict, summary: str, model: str) -> int:
    body = {k: card[k] for k in ("dims", "fair", "sources", "options_stamp", "label")}
    return db.execute(
        "INSERT INTO scorecards(symbol, created_at, as_of, price, overall, tone, dims, weights_ver, summary, summary_model) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (card["symbol"], utcnow_iso(), card["as_of"], card["price"], card["overall"], card["tone"],
         json.dumps(body, ensure_ascii=False, default=str), card["weights_ver"], summary, model))


def run(symbol: str, use_llm: bool = True, x: hub.Inputs | None = None) -> int:
    card = compute(symbol, x)
    summary, model = llm_summary(card) if use_llm and card["overall"] is not None else (template_summary(card), "şablon")
    return save(card, summary, model)


def latest(symbol: str) -> dict | None:
    r = db.query_one("SELECT * FROM scorecards WHERE symbol = ? ORDER BY id DESC LIMIT 1", (symbol.upper(),))
    return load(r) if r else None


def load(r) -> dict:
    d = dict(r)
    body = json.loads(d.pop("dims"))
    d.update(body)
    return d


def history(symbol: str, limit: int = 30) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT id, created_at, as_of, price, overall, tone FROM scorecards WHERE symbol = ? ORDER BY id DESC LIMIT ?",
        (symbol.upper(), limit))]


# ── track record ──────────────────────────────────────────

def track_record(horizon: int = 20) -> dict:
    """Forward `horizon`-session return vs SPY per band, one card per symbol per day."""
    cards = db.query("""SELECT symbol, as_of, price, overall FROM scorecards WHERE as_of IS NOT NULL AND price IS NOT NULL
                        AND id IN (SELECT MAX(id) FROM scorecards GROUP BY symbol, as_of)""")
    spy = prices.series(config.CALENDAR_SYMBOL)
    out = {tone: {"tone": tone, "label": label, "n": 0, "hits": 0, "sum": 0.0} for _, tone, label in BANDS}
    cache: dict[str, prices.Series] = {}
    for c in cards:
        if c["as_of"] not in spy.days:
            continue
        i = spy.days.index(c["as_of"])
        if i + horizon >= len(spy.days):
            continue
        end = spy.days[i + horizon]
        s = cache.setdefault(c["symbol"], prices.series(c["symbol"]))
        p1 = s.at(end)
        if not p1:
            continue
        excess = (p1 / c["price"] - 1) * 100 - (spy.closes[i + horizon] / spy.closes[i] - 1) * 100
        tone, _ = band(c["overall"])
        b = out[tone]
        b["n"] += 1
        b["sum"] += excess
        b["hits"] += 1 if (excess > 0) == (tone != "neg") else 0
    rows = []
    for b in out.values():
        rows.append({**b, "avg": b["sum"] / b["n"] if b["n"] else None, "hit": b["hits"] / b["n"] * 100 if b["n"] else None})
    return {"horizon": horizon, "rows": rows, "n": sum(b["n"] for b in rows), "cards": len(cards)}


def daily_job() -> int:
    """Template-summary card per holding + watchlist symbol, once per price day (builds the track record)."""
    from . import portfolio
    syms = {p["symbol"] for p in portfolio.positions() if p["asset"] == "stock"} | {t["symbol"] for t in db.active_tickers()}
    n = 0
    for s in sorted(syms):
        try:
            last = db.query_one("SELECT as_of FROM scorecards WHERE symbol = ? ORDER BY id DESC LIMIT 1", (s,))
            spy_last = prices.last_update()
            if last and spy_last and last["as_of"] == spy_last["day"]:
                continue
            run(s, use_llm=False)
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("scorecard %s failed", s)
    return n
