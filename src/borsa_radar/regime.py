"""Market regime from transparent rules (v1).

Seven components, each scored 0-100 from the shared bar store and the breadth
table, weighted into one score and a label. Plus two slower/faster gauges:
"İklim" (0-10, long-term ground) and "Hava" (−8..+8, four short-term signals).
Everything is computed for a single as-of day T; history is backfilled by
recomputing past days from stored bars so the track record ("sicil") exists
from day one. The model may only add one summary sentence (number-guarded).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from . import breadth, config, db, prices
from .util import utcnow_iso

log = logging.getLogger(__name__)
RULES_VER = "v1"
WEIGHTS = {"trend": 25, "vol": 20, "breadth": 20, "credit": 15, "particip": 10, "curve": 5, "dollar": 5}
NAMES = {"trend": "Trend (S&P 500)", "vol": "Oynaklık (VIX)", "breadth": "Genişlik", "credit": "Kredi (HYG/LQD)",
         "particip": "Katılım (RSP, IWM)", "curve": "Getiri eğrisi", "dollar": "Dolar"}
EXPLAIN = {
    "trend": "S&P 500 fonunun (SPY) 20, 50 ve 200 günlük ortalamalara göre yeri ve 200 günlükten uzaklığı. Uzun trend yukarıysa zemin sağlamdır.",
    "vol": "VIX, opsiyon fiyatlarından okunan önümüzdeki 30 günlük beklenen dalgalanma. 20 altı sakin, 30 üstü panik. VIX 3 aylık VIX'in üstüne çıkarsa (ters vade yapısı) kısa vadeli korku yüksektir.",
    "breadth": "S&P 500 hisselerinin kaçı kendi 50 ve 200 günlük ortalamasının üstünde. Endeksi birkaç dev şirket mi taşıyor, yoksa geniş katılım mı var?",
    "credit": "Yüksek getirili (riskli) şirket tahvilleri ile yatırım yapılabilir tahviller oranı. Kredi piyasası hisselerden önce bozulur; oran düşüyorsa risk iştahı azalıyor.",
    "particip": "Eşit ağırlıklı S&P 500 (RSP) ve küçük şirketler (IWM) büyük endeksin önünde mi? Öndeyse yükselişe ortalama hisse de katılıyor.",
    "curve": "10 yıllık faiz eksi 3 aylık faiz. Negatifse (ters eğri) tarihsel olarak resesyon riski; hızlı faiz sıçraması hisseleri baskılar.",
    "dollar": "Dolar endeksinin son 20 gündeki yönü. Sert dolar yükselişi küresel risk iştahını daraltır.",
}
LABELS = [(70, "Risk-on Boğa", "pos"), (58, "Temkinli Boğa", "pos"), (45, "Nötr-Dağınık", "warn"), (32, "Risk-off", "neg"), (0, "Ayı", "neg")]
BULLISH = {"Risk-on Boğa", "Temkinli Boğa"}


def clamp(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def lin(v: float, x0: float, y0: float, x1: float, y1: float) -> float:
    """Linear map of v from (x0→y0) to (x1→y1), clamped to the y range."""
    if x1 == x0:
        return y0
    y = y0 + (v - x0) * (y1 - y0) / (x1 - x0)
    return clamp(y, min(y0, y1), max(y0, y1))


# ── component rules (pure) ───────────────────────────────

def score_trend(px: float, s20: float, s50: float, s200: float) -> tuple[float, list[str]]:
    dist = (px / s200 - 1) * 100
    pts = 15 * (px > s20) + 15 * (px > s50) + 25 * (px > s200) + 15 * (s50 > s200) + lin(dist, -8, 0, 8, 30)
    why = [f"SPY 20/50/200 günlük ortalamaların {'üstünde' if px > s20 else 'altında'}/{'üstünde' if px > s50 else 'altında'}/"
           f"{'üstünde' if px > s200 else 'altında'}", f"200 günlükten uzaklık %{dist:+.1f}"]
    return round(pts, 1), why


def score_vol(vix: float, vix3m: float | None) -> tuple[float, list[str]]:
    s = lin(vix, 12, 95, 32, 5)
    why = [f"VIX {vix:.1f}"]
    if vix3m:
        ts = vix / vix3m
        s += 8 if ts < 0.85 else 0 if ts < 1.0 else -20
        why.append(f"VIX/VIX3M {ts:.2f}" + (" (ters vade yapısı: kısa vadeli korku)" if ts >= 1 else ""))
    return round(clamp(s), 1), why


def score_breadth(above200: float | None, above50: float | None, mcclellan: float | None,
                  sector_pct: float | None = None) -> tuple[float | None, list[str]]:
    if above200 is None:
        if sector_pct is None:
            return None, []
        return round(lin(sector_pct, 20, 10, 90, 90), 1), [f"Sektör fonlarının %{sector_pct:.0f}'i 200 günlük üstünde (yedek ölçü)"]
    s = 0.6 * lin(above200, 30, 5, 75, 95) + 0.4 * lin(above50 if above50 is not None else above200, 25, 5, 75, 95)
    why = [f"Hisselerin %{above200:.1f}'i 200g, %{above50:.1f}'i 50g ortalamasının üstünde" if above50 is not None
           else f"Hisselerin %{above200:.1f}'i 200g üstünde"]
    if mcclellan is not None:
        s += 5 if mcclellan > 20 else -5 if mcclellan < -20 else 0
        why.append(f"McClellan {mcclellan:+.0f}")
    return round(clamp(s), 1), why


def score_ratio_trend(ratio: float, sma50: float, chg20: float) -> float:
    """Generic: ratio above its 50-day average and rising → high."""
    return clamp(50 + (12 if ratio > sma50 else -12) + lin(chg20, -3, -25, 3, 25))


def score_curve(spread: float, tnx_chg20_bp: float) -> tuple[float, list[str]]:
    s = lin(spread, -1.0, 25, 1.5, 80)
    why = [f"10Y−3A farkı {spread:+.2f} puan" + (" (ters eğri)" if spread < 0 else "")]
    if tnx_chg20_bp > 40:
        s -= 15
        why.append(f"10Y faiz 20 günde +{tnx_chg20_bp:.0f} baz puan (faiz şoku)")
    return round(clamp(s), 1), why


def score_dollar(chg20: float) -> tuple[float, list[str]]:
    return round(lin(chg20, -3, 75, 3, 25), 1), [f"Dolar endeksi 20 günde %{chg20:+.1f}"]


def score_particip(rsp_rel: float, iwm_rel: float) -> tuple[float, list[str]]:
    s = 50 + lin(rsp_rel, -4, -25, 4, 25) + lin(iwm_rel, -6, -15, 6, 15)
    return round(clamp(s), 1), [f"RSP SPY'a göre 3 ayda %{rsp_rel:+.1f}", f"IWM SPY'a göre 3 ayda %{iwm_rel:+.1f}"]


def combine(scores: dict[str, float | None], weights: dict[str, float] = WEIGHTS) -> float | None:
    have = [(scores[k], w) for k, w in weights.items() if scores.get(k) is not None]
    tw = sum(w for _, w in have)
    return round(sum(s * w for s, w in have) / tw, 1) if tw else None


def label_for(overall: float | None, spy_above200: bool, vix: float | None) -> tuple[str, str]:
    if overall is None:
        return "doğrulanamadı", "neu"
    lab, tone = next((l, t) for cut, l, t in LABELS if overall >= cut)
    # hard guards: no bull label below the 200-day; panic VIX caps at Risk-off
    if not spy_above200 and lab in BULLISH:
        lab, tone = "Nötr-Dağınık", "warn"
    if vix is not None and vix >= 30 and lab not in ("Risk-off", "Ayı"):
        lab, tone = "Risk-off", "neg"
    return lab, tone


def weather_points(spy_vs20: float, vix_chg5: float, net_ad5: float | None, vix_term: float | None) -> list[tuple[str, int]]:
    """Four fast signals, each −2..+2."""
    def step(v, cuts):  # cuts ascending: (-2 below c0, -1 below c1, 0 below c2, +1 below c3, else +2)
        return -2 + sum(v >= c for c in cuts)
    out = [("SPY 20 günlüğe göre", step(spy_vs20, (-2.5, -0.75, 0.75, 2.5))),
           ("VIX 5 günlük değişim", -step(vix_chg5, (-15, -5, 5, 15)))]
    if net_ad5 is not None:
        out.append(("5 günlük net yükselen−düşen", step(net_ad5, (-600, -150, 150, 600))))
    if vix_term is not None:
        out.append(("VIX vade yapısı", 2 if vix_term < 0.85 else 1 if vix_term < 0.92 else 0 if vix_term < 0.98 else -1 if vix_term < 1.03 else -2))
    return out


# ── market data at T ─────────────────────────────────────

@dataclass
class Market:
    days: list[str]
    close: pd.DataFrame      # raw closes, SPY calendar, forward-filled
    adj: pd.DataFrame        # dividend-adjusted (ratios of income ETFs)
    volume: pd.DataFrame
    breadth: pd.DataFrame    # breadth_daily indexed by day


def load(start: str | None = None) -> Market:
    start = start or (date.today() - timedelta(days=3 * 366)).isoformat()
    syms = config.market_universe()
    rows = db.query(f"SELECT symbol, day, close, adj_close, volume FROM prices_daily WHERE day >= ? "
                    f"AND symbol IN ({','.join('?' * len(syms))})", [start, *syms])
    cal = prices.trading_days(start)
    df = pd.DataFrame([tuple(r) for r in rows], columns=["symbol", "day", "close", "adj", "volume"])
    if df.empty or not cal:
        e = pd.DataFrame(index=cal)
        return Market(cal, e, e, e, pd.DataFrame())

    def piv(col):
        return df.pivot_table(index="day", columns="symbol", values=col, aggfunc="last").reindex(cal).ffill()
    b = pd.DataFrame(breadth.history(start)).set_index("day") if breadth.history(start) else pd.DataFrame()
    adj = piv("adj")
    close = piv("close")
    adj = adj.fillna(close)
    return Market(cal, close, adj, df.pivot_table(index="day", columns="symbol", values="volume", aggfunc="last").reindex(cal), b)


def _col(m: Market, frame: str, sym: str, i: int, n: int | None = None):
    f = getattr(m, frame)
    if sym not in f.columns:
        return None
    s = f[sym].iloc[: i + 1].dropna()
    if s.empty:
        return None
    return s if n is None else (s.iloc[-n:] if len(s) >= n else None)


def _sma(s: pd.Series | None, n: int) -> float | None:
    return float(s.iloc[-n:].mean()) if s is not None and len(s) >= n else None


def _chg(s: pd.Series | None, n: int) -> float | None:
    if s is None or len(s) <= n or not s.iloc[-n - 1]:
        return None
    return float((s.iloc[-1] / s.iloc[-n - 1] - 1) * 100)


def compute(m: Market, i: int) -> dict | None:
    """Regime at calendar index i (T = m.days[i])."""
    day = m.days[i]
    spy = _col(m, "close", "SPY", i)
    if spy is None or len(spy) < 200:
        return None
    px, s20, s50, s200 = float(spy.iloc[-1]), _sma(spy, 20), _sma(spy, 50), _sma(spy, 200)
    comps: dict[str, dict] = {}
    missing = []

    sc, why = score_trend(px, s20, s50, s200)
    comps["trend"] = {"score": sc, "why": why,
                      "trigger": f"SPY 200 günlük ({s200:.2f}) altına kapanırsa trend puanı ~25 puan düşer" if px > s200
                      else f"SPY 200 günlük ({s200:.2f}) üstüne kapanırsa trend puanı ~25 puan artar",
                      "levels": {"spy": px, "sma20": s20, "sma50": s50, "sma200": s200}}

    vixs = _col(m, "close", "^VIX", i)
    vix = float(vixs.iloc[-1]) if vixs is not None else None
    v3 = _col(m, "close", "^VIX3M", i)
    vix3m = float(v3.iloc[-1]) if v3 is not None else None
    if vix is not None:
        sc, why = score_vol(vix, vix3m)
        comps["vol"] = {"score": sc, "why": why, "trigger": "VIX 20 üstü kapanış oynaklık puanını 'gergin' bölgeye taşır; 30 üstü etiketi en fazla Risk-off yapar"
                        if vix < 20 else "VIX 20 altına inerse oynaklık puanı belirgin artar", "levels": {"vix": vix, "vix3m": vix3m}}
    else:
        missing.append("VIX")

    br = None
    if not m.breadth.empty:
        prior = m.breadth.loc[:day]
        if len(prior) and (date.fromisoformat(day) - date.fromisoformat(prior.index[-1])).days <= 5:
            br = prior.iloc[-1]
    sector_pct = None
    if br is None:
        cnt = have = 0
        for s in config.SECTOR_ETFS:
            col = _col(m, "adj", s, i)
            if col is not None and len(col) >= 200:
                have += 1
                cnt += float(col.iloc[-1]) > _sma(col, 200)
        sector_pct = cnt / have * 100 if have else None
    sc, why = score_breadth(None if br is None else _nan(br["above200"]), None if br is None else _nan(br["above50"]),
                            None if br is None else _nan(br["mcclellan"]), sector_pct)
    if sc is not None:
        comps["breadth"] = {"score": sc, "why": why, "trigger": "200 günlük üstündeki hisse oranı %50 altına inerse genişlik zayıf sayılır; %70 üstü güçlü",
                            "levels": {"above200": None if br is None else _nan(br["above200"]), "sector_pct": sector_pct}}
    else:
        missing.append("Genişlik")

    hyg, lqd = _col(m, "adj", "HYG", i), _col(m, "adj", "LQD", i)
    if hyg is not None and lqd is not None and len(hyg) >= 60 and len(lqd) >= 60:
        ratio = (hyg / lqd).dropna()
        r_now, r50, r20 = float(ratio.iloc[-1]), _sma(ratio, 50), _chg(ratio, 20)
        sc = round(score_ratio_trend(r_now, r50, r20), 1)
        comps["credit"] = {"score": sc, "why": [f"HYG/LQD 50 günlük ortalamasının {'üstünde' if r_now > r50 else 'altında'}", f"20 günde %{r20:+.1f}"],
                           "trigger": f"HYG/LQD oranı 50 günlük ortalaması ({r50:.4f}) {'altına' if r_now > r50 else 'üstüne'} geçerse kredi puanı ~24 puan {'düşer' if r_now > r50 else 'artar'}",
                           "levels": {"ratio": r_now, "sma50": r50}}
    else:
        missing.append("Kredi")

    rsp, iwm = _col(m, "adj", "RSP", i), _col(m, "adj", "IWM", i)
    spya = _col(m, "adj", "SPY", i)
    if rsp is not None and iwm is not None and len(rsp) > 63 and len(iwm) > 63:
        rr = _chg(rsp, 63) - _chg(spya, 63)
        ir = _chg(iwm, 63) - _chg(spya, 63)
        sc, why = score_particip(rr, ir)
        comps["particip"] = {"score": sc, "why": why, "trigger": "RSP ve IWM'in SPY'ı geçmesi katılım puanını yükseltir; ikisi de gerilerse yükseliş dar tabanlı kalır",
                             "levels": {"rsp_rel": rr, "iwm_rel": ir}}
    else:
        missing.append("Katılım")

    tnx, irx = _col(m, "close", "^TNX", i), _col(m, "close", "^IRX", i)
    if tnx is not None and irx is not None and len(tnx) > 20:
        spread = float(tnx.iloc[-1] - irx.iloc[-1])
        bp = float((tnx.iloc[-1] - tnx.iloc[-21]) * 100)
        sc, why = score_curve(spread, bp)
        comps["curve"] = {"score": sc, "why": why, "trigger": "Eğrinin negatife dönmesi veya 10Y faizin 20 günde 40 baz puandan fazla yükselmesi puanı düşürür",
                          "levels": {"tnx": float(tnx.iloc[-1]), "irx": float(irx.iloc[-1]), "spread": spread, "tnx_chg20_bp": bp}}
    else:
        missing.append("Getiri eğrisi")

    dxy = _col(m, "close", "DX-Y.NYB", i)
    if dxy is not None and len(dxy) > 20:
        c20 = _chg(dxy, 20)
        sc, why = score_dollar(c20)
        comps["dollar"] = {"score": sc, "why": why, "trigger": "Dolar endeksinin 20 günde %3'ten fazla yükselmesi puanı en alta çeker",
                           "levels": {"dxy": float(dxy.iloc[-1]), "chg20": c20}}
    else:
        missing.append("Dolar")

    for k, c in comps.items():
        c["name"], c["weight"], c["explain"] = NAMES[k], WEIGHTS[k], EXPLAIN[k]
        c["tone"] = "pos" if c["score"] >= 60 else "neg" if c["score"] < 40 else "warn"
    overall = combine({k: c["score"] for k, c in comps.items()})
    label, tone = label_for(overall, px > s200, vix)

    # slow gauge: long-term ground (0-10)
    slow = [comps[k]["score"] for k in ("credit", "curve") if k in comps]
    slow.append(lin((px / s200 - 1) * 100, -10, 0, 10, 100))
    if br is not None and _nan(br["above200"]) is not None:
        slow.append(lin(_nan(br["above200"]), 30, 0, 75, 100))
    climate = round(sum(slow) / len(slow) / 10, 2)

    # fast gauge: four short-term signals (−8..+8)
    net5 = None
    if not m.breadth.empty:
        recent = m.breadth.loc[:day].tail(5)
        if len(recent) == 5:
            net5 = float((recent["adv"] - recent["dec"]).sum())
    wpts = weather_points((px / s20 - 1) * 100, _chg(vixs, 5) or 0 if vixs is not None else 0, net5,
                          (vix / vix3m) if vix and vix3m else None)
    weather = sum(p for _, p in wpts)

    return {"day": day, "overall": overall, "label": label, "tone": tone, "climate": climate, "weather": weather,
            "weather_parts": wpts, "comps": comps, "missing": missing, "rules_ver": RULES_VER,
            "breadth_source": "S&P 500 hisseleri" if br is not None else "11 sektör fonu (yedek)" if sector_pct is not None else None}


def _nan(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def climate_label(c: float) -> str:
    return "Sıcak (risk iştahı yüksek)" if c >= 7 else "Ilımlı" if c >= 5 else "Serin" if c >= 3.5 else "Soğuk (risk zemini zayıf)"


def weather_label(w: float) -> str:
    return "Güneşli" if w >= 4 else "Açık" if w >= 1 else "Değişken" if w >= -1 else "Gergin" if w >= -4 else "Fırtınalı"


# ── persistence / history ────────────────────────────────

def _store(r: dict, summary: str | None = None, model: str | None = None) -> None:
    body = {k: r[k] for k in ("comps", "missing", "weather_parts", "breadth_source")}
    db.execute("INSERT INTO regime_history(day, overall, label, tone, climate, weather, comps, rules_ver, summary, summary_model, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET overall=excluded.overall, label=excluded.label, "
               "tone=excluded.tone, climate=excluded.climate, weather=excluded.weather, comps=excluded.comps, rules_ver=excluded.rules_ver, "
               "summary=COALESCE(excluded.summary, regime_history.summary), summary_model=COALESCE(excluded.summary_model, regime_history.summary_model), "
               "created_at=excluded.created_at",
               (r["day"], r["overall"], r["label"], r["tone"], r["climate"], r["weather"],
                json.dumps(body, ensure_ascii=False, default=str), RULES_VER, summary, model, utcnow_iso()))


def backfill(m: Market | None = None, only_missing: bool = False) -> int:
    m = m or load()
    have = {r["day"] for r in db.query("SELECT day FROM regime_history WHERE rules_ver = ?", (RULES_VER,))} if only_missing else set()
    n = 0
    for i, d in enumerate(m.days):
        if d in have:
            continue
        r = compute(m, i)
        if r:
            _store(r)
            n += 1
    return n


def latest() -> dict | None:
    r = db.query_one("SELECT * FROM regime_history ORDER BY day DESC LIMIT 1")
    return load_row(r) if r else None


def load_row(r) -> dict:
    d = dict(r)
    d.update(json.loads(d.pop("comps")))
    return d


def history(start: str | None = None) -> list[dict]:
    return [dict(r) for r in db.query("SELECT day, overall, label, tone, climate, weather FROM regime_history WHERE day >= ? ORDER BY day",
                                      (start or "0000",))]


def track_record(horizon: int = 20) -> dict:
    """SPY forward `horizon`-session return per label; also on non-overlapping samples (every `horizon` days)."""
    hist = history()
    spy = prices.series("SPY")
    idx = {d: k for k, d in enumerate(spy.days)}
    stats: dict[str, dict] = {}
    for j, h in enumerate(hist):
        k = idx.get(h["day"])
        if k is None or k + horizon >= len(spy.days) or not h["label"]:
            continue
        fwd = (spy.closes[k + horizon] / spy.closes[k] - 1) * 100
        s = stats.setdefault(h["label"], {"label": h["label"], "tone": h["tone"], "n": 0, "sum": 0.0, "pos": 0, "ind": [], "last": None})
        s["n"] += 1
        s["sum"] += fwd
        s["pos"] += fwd > 0
        if s["last"] is None or k - s["last"] >= horizon:
            s["ind"].append(fwd)
            s["last"] = k
    rows = []
    for _, lab, tone in LABELS:
        s = stats.get(lab)
        if not s:
            rows.append({"label": lab, "tone": tone, "n": 0, "n_ind": 0, "avg": None, "pos": None, "avg_ind": None})
            continue
        ind = s["ind"]
        rows.append({"label": lab, "tone": tone, "n": s["n"], "n_ind": len(ind), "avg": s["sum"] / s["n"], "pos": s["pos"] / s["n"] * 100,
                     "avg_ind": sum(ind) / len(ind) if ind else None,
                     "hit_ind": (sum(1 for x in ind if (x > 0) == (lab in BULLISH or lab == "Nötr-Dağınık")) / len(ind) * 100) if ind else None})
    base = [(spy.closes[k + horizon] / spy.closes[k] - 1) * 100 for k in range(len(spy.days) - horizon)
            if hist and spy.days[k] >= hist[0]["day"]]
    return {"horizon": horizon, "rows": rows, "days": len(hist), "first": hist[0]["day"] if hist else None,
            "base_avg": sum(base) / len(base) if base else None, "base_pos": sum(1 for x in base if x > 0) / len(base) * 100 if base else None}


# ── summary sentence ─────────────────────────────────────

def template_summary(r: dict) -> str:
    comps = sorted(r["comps"].values(), key=lambda c: c["score"])
    weak, strong = comps[0], comps[-1]
    return (f"Piyasa rejimi {r['label']} ({r['overall']:.0f}/100); en güçlü ayak {strong['name'].lower()} ({strong['score']:.0f}), "
            f"en zayıf ayak {weak['name'].lower()} ({weak['score']:.0f}).")


def llm_summary(r: dict, pulse_facts: dict | None = None) -> tuple[str, str]:
    from . import llm
    from .scorecard import _numbers
    facts = {"rejim": r["label"], "puan": round(r["overall"]), "iklim_0_10": round(r["climate"], 1), "hava_-8_8": r["weather"],
             "bilesenler": {c["name"]: {"puan": round(c["score"]), "gerekce": c["why"]} for c in r["comps"].values()}}
    if pulse_facts:
        facts["gun"] = pulse_facts
    payload = json.dumps(facts, ensure_ascii=False)
    prompt = ("Aşağıdaki ABD piyasa rejimi verisini Türkçe TEK cümleyle yorumla (en fazla 35 kelime), 'Yani' ile başla. "
              "Yalnızca verilen sayıları kullan; yeni sayı, tarih, yüzde uydurma; al/sat tavsiyesi verme. Sadece cümleyi yaz.\n\n" + payload)
    try:
        text, model = llm.complete(prompt, timeout=120, budget="daily")
    except llm.LLMError as exc:
        log.info("regime summary fallback: %s", exc)
        return template_summary(r), "şablon"
    text = " ".join(text.strip().strip('"').split())
    bad = _numbers(text) - _numbers(payload) - {"0", "8", "10", "100", "200", "50", "20"}
    if bad or not text or len(text) > 400:
        log.warning("regime summary rejected (%s): %s", bad, text[:200])
        return template_summary(r), "şablon (model metni doğrulanamadı)"
    return text, model


def snapshot(use_llm: bool = True) -> dict | None:
    """Daily job: (re)compute the last day, fill gaps in history, write the summary sentence."""
    m = load()
    if not m.days:
        return None
    backfill(m, only_missing=True)
    r = compute(m, len(m.days) - 1)
    if not r:
        return None
    from . import pulse
    summary, model = llm_summary(r, pulse.facts_for_summary(r["day"])) if use_llm else (template_summary(r), "şablon")
    _store(r, summary, model)
    return latest()
