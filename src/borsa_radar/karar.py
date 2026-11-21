"""Verdict layer ("Karar"): one explainable conclusion per held stock and one for the portfolio.

The karne averages six dimensions, which hides conflicts (great business + broken chart lands in "Karışık").
Here the stock is read on two axes instead: business (karne "temel", estimate cuts, analyst actions, dilution /
insider flags) × price (50/200-day averages, 3-month strength vs S&P, distance from the 52-week high). The axes
name a pattern; thesis state, position context, distinct-story news tone, the market regime vs the stock's beta
and dated catalysts are layered on top, and a fixed ladder turns the signals into a stance:

    alarm    own exit rule triggered (always wins)
    sorgula  price broken AND (business weak OR loss ≤ LOSS_DEEP without a written exit rule)
    izle     anything that is neither sorgula nor saglam (conflicting axes or a red flag)
    saglam   business not weak, price not broken, no red flag
    veri_yok no stored scorecard or no price history

`gather` reads only stored data (nightly karne, bar store, ledger, theses, events, calendar); `decide` is pure
and is what the tests hit. "changers" are derived from the same thresholds, so every stance can be reproduced
and every move predicted. Stances are states, never orders. Cards are stored with their price so the forward
excess return per stance can be measured ("sicil"). The model only writes short comments on top; a comment
with a number that is not in its facts is dropped and the UI falls back to the server-built headline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta

from . import config, db, hub, prices
from .util import iso, tr_n, utcnow, utcnow_iso

log = logging.getLogger(__name__)

RULES_VER = "v1"
STANCES = {  # key: (label_tr, tone, rank) — higher rank = needs more attention
    "alarm": ("Kural ihlali", "neg", 3), "sorgula": ("Tezi sorgula", "neg", 2), "izle": ("İzle", "warn", 1),
    "saglam": ("Sağlam", "pos", 0), "veri_yok": ("Yetersiz veri", "neu", -1),
}
PATTERNS = {  # (business, price) → (key, label)
    ("guclu", "guclu"): ("ikisi_guclu", "Sağlam şirket, sağlam grafik"),
    ("guclu", "orta"): ("sirket_guclu", "Sağlam şirket, kararsız grafik"),
    ("guclu", "bozuk"): ("sirket_guclu_fiyat_zayif", "Sağlam şirket, bozuk grafik"),
    ("orta", "guclu"): ("fiyat_guclu", "Ortalama şirket, güçlü grafik"),
    ("orta", "orta"): ("karisik", "Karışık tablo"),
    ("orta", "bozuk"): ("fiyat_zayif", "Ortalama şirket, bozuk grafik"),
    ("zayif", "guclu"): ("sirket_zayif_fiyat_guclu", "Zayıf şirket, güçlü grafik"),
    ("zayif", "orta"): ("sirket_zayif", "Zayıf şirket, kararsız grafik"),
    ("zayif", "bozuk"): ("ikisi_zayif", "Zayıf şirket, bozuk grafik"),
    ("bilinmiyor", "guclu"): ("sirket_bilinmiyor_fiyat_guclu", "Şirket verisi eksik, güçlü grafik"),
    ("bilinmiyor", "orta"): ("sirket_bilinmiyor", "Şirket verisi eksik, kararsız grafik"),
    ("bilinmiyor", "bozuk"): ("sirket_bilinmiyor_fiyat_zayif", "Şirket verisi eksik, bozuk grafik"),
}

# ── thresholds (named so changers and tests use the same numbers) ──
BUS_STRONG = 67          # karne "temel" ≥ → business strong (karne band edge "Güçlü")
BUS_WEAK = 45            # karne "temel" < → business weak (karne band edge "Zayıf")
REV_EPS = 3.0            # 2-quarter EPS estimate revision over 30 days, % (± → signal)
REV_TARGET = 10.0        # analyst target trend month vs quarter, % (± → signal)
GRADES_MIN = 3           # ≥ this many downgrades (and ≥ 2× upgrades) in 30 days → signal
BIZ_NEG_STEP = 2         # estimate cut + at least this many negative business signals → business one step down
RS_WEAK, RS_STRONG = -15.0, 15.0     # 3-month relative strength vs S&P, % points
FROM_HI_DEEP = -35.0     # % below the 52-week high
LOSS_DEEP = -25.0        # unrealized % at or below → deep loss
GAIN_BIG = 50.0          # unrealized % at or above → note
WEIGHT_HIGH = 20.0       # position weight % (= risk.LIMITS["single"])
RISK_SHARE_X = 1.5       # share of portfolio risk ≥ this × weight → signal
CLUSTER_HIGH = 40.0      # correlated cluster weight % (= risk.LIMITS["cluster"])
NEWS_DAYS, NEWS_MIN_IMP, NEWS_MIN_STORIES = 14, 3, 3
NEWS_SIM_DAYS = 2        # headline-similarity grouping window (days) for events without a story key
NEWS_TONE = 0.35         # |importance-weighted net direction of distinct stories| ≥ → signal (negative = red flag)
REGIME_RISKOFF = 45      # regime score < → Risk-off (regime.LABELS)
REGIME_BULL = 58         # regime score ≥ → bullish label
BETA_HIGH = 1.3
NET_OP_GAP = 25.0         # SEC fallback: net vs operating margin gap (% points) above which net margin is ignored
SEC_MARGIN_MAX = 300.0   # |margin| % above which P&L is several times revenue: not an operating picture, no SEC score
DILUTION = 3.0           # shares outstanding year over year, % (same edge as the karne anomali part)
VAL_EDGE = 20.0          # fair value upside ±% → valuation signal (only when confidence is not "düşük")
EARN_SOON = 7            # days: earnings inside → note
NEXT_DAYS = 30           # days: dated catalysts listed in "next"
AGENDA_SORGULA = 2       # 'sorgula' names listed one by one on the agenda; the rest are grouped
COMMENT_TTL = 7          # days a model comment may be reused while the fingerprint is unchanged
EXIT_KINDS = {"price_below", "loss_pct", "karne_below", "rev_growth_below", "dilution_above", "regime_below", "deadline"}
FLAG_TR = {"sulandirma": "hisse sayısı artıyor (sulandırma)", "iceriden_satis": "yoğun içeriden satış", "haber_negatif": "haber tonu negatif",
           "tez_gozden_gecirme": "tez gözden geçirme tarihi geldi", "rejim_beta": "risk-off rejimde yüksek beta"}
ALLOW_NUMS = {"0", "1", "2", "3", "5", "7", "14", "20", "30", "50", "100", "200"}
BANNED = re.compile(r"\b(al|alın|alabilirsin|sat|satın|satabilirsin|ekle|ekleyin|azalt|azaltın|pozisyonu kapat)\b", re.I)


def _usd(v) -> str:
    return f"{tr_n(v, 2)}$"


def _p(v, d=1, sign=True) -> str:
    return hub.pct(v, d, sign)


def _pnum(s) -> float | None:
    """'%+105,9' → 105.9 (karne part values are Turkish-formatted strings)."""
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(s or ""))
    return float(m.group().replace(",", ".")) if m else None


def _part(card: dict | None, dim: str, prefix: str) -> dict | None:
    d = ((card or {}).get("dims") or {}).get(dim) or {}
    return next((p for p in d.get("parts") or [] if str(p.get("label", "")).startswith(prefix)), None)


def _score(card: dict | None, dim: str) -> float | None:
    return (((card or {}).get("dims") or {}).get(dim) or {}).get("score")


def _sig(key: str, text: str, w: int, src: str) -> dict:
    return {"key": key, "text": text, "w": w, "src": src}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


# ── news: distinct stories ───────────────────────────────

def _same(wa: set, wb: set) -> bool:
    """notifier.similar on precomputed word sets (Jaccard ≥ 0.5 of the headline words)."""
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def stories(events: list[dict]) -> list[dict]:
    """Group events into stories (several outlets, one story): the analyzer's story key when present, otherwise the
    notifier's headline-similarity rule (same symbol, within NEWS_SIM_DAYS, similar headline) or an identical title key.
    Each group then counts once: max importance, importance-weighted mean direction."""
    from .notifier import _words
    groups: list[dict] = []
    by_key: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: e.get("at") or ""):
        imp = e.get("imp") or 1
        words = _words(e.get("title") or "")
        g = by_key.get(e["story"]) if e.get("story") else None
        if g is None and not e.get("story"):
            g = by_key.get("t:" + e["tk"]) if e.get("tk") else None
            if g is None:
                g = next((x for x in reversed(groups) if abs(_days(x["last"], e.get("at") or x["last"])) <= NEWS_SIM_DAYS
                          and any(_same(words, w) for w in x["words"])), None)
        if g is None:
            g = {"story": e.get("story") or (f"t:{e['tk']}" if e.get("tk") else f"g:{len(groups)}"), "imp": 0, "wsum": 0.0, "dsum": 0.0, "n": 0,
                 "title": e.get("title"), "at": e.get("at"), "last": e.get("at") or "", "words": []}
            groups.append(g)
        for k in (e.get("story"), ("t:" + e["tk"]) if e.get("tk") else None):
            if k:
                by_key.setdefault(k, g)
        g["n"] += 1
        g["wsum"] += imp
        g["dsum"] += (e.get("dir") or 0) * imp
        g["last"] = max(g["last"], e.get("at") or "")
        if len(g["words"]) < 8 and words:
            g["words"].append(words)
        if imp > g["imp"]:
            g["imp"], g["title"], g["at"] = imp, e.get("title"), e.get("at")
    for g in groups:
        g["dir"] = g["dsum"] / g["wsum"] if g["wsum"] else 0.0
        del g["words"]
    return sorted(groups, key=lambda g: (-g["imp"], -abs(g["dir"])))


def news_tone(events: list[dict]) -> dict:
    """Tone over groups (one story = one vote, whatever the number of outlets). `keyed` = share of events with a story key;
    below one half the groups come from headline similarity and are called "grup", not "ayrı hikâye"."""
    st = stories(events)
    w = sum(s["imp"] for s in st)
    keyed = sum(1 for e in events if e.get("story")) / len(events) if events else 0.0
    return {"stories": st, "n": len(st), "events": len(events), "keyed": keyed, "by_story": keyed >= 0.5,
            "net": sum(s["dir"] * s["imp"] for s in st) / w if w else None}


# ── SEC quarterly fallback for the business axis ─────────

def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def sec_temel(quarters: list[dict] | None) -> dict | None:
    """Temel-equivalent 0-100 from stored SEC quarters (newest first), using the karne's own formulas:
    revenue growth clamp(40 + g·2), net margin and operating margin clamp(30 + m·250). Margins are trailing four
    quarters when four quarters with revenue exist, else the latest quarter. Needs ≥ 2 computable parts, else None.
    Also returns the year-over-year share count change (dilution check)."""
    q = [r for r in quarters or [] if r.get("period_end")]
    if not q or not q[0].get("revenue") or q[0]["revenue"] <= 0:
        return None
    r0 = q[0]
    parts = []
    g = r0.get("rev_yoy")
    if g is not None:
        parts.append(("gelir büyümesi (yıllık)", _p(g), _clamp(40 + g * 2)))
    four = q[:4]
    ttm = len(four) == 4 and all((r.get("revenue") or 0) > 0 for r in four)
    base = four if ttm else [r0]
    rev = sum(r["revenue"] for r in base)
    lab = "son 4 çeyrek" if ttm else "son çeyrek"
    mg = {key: sum(r[key] for r in base) / rev for key in ("net_income", "op_income") if all(r.get(key) is not None for r in base)}
    if len(mg) == 2 and abs(mg["net_income"] - mg["op_income"]) * 100 > NET_OP_GAP:
        mg.pop("net_income")      # net result dominated by non-operating items (revaluations, one-offs): operating margin only
    yo = next((r for r in q[1:] if 350 <= _days(r["period_end"], r0["period_end"]) <= 380), None)
    dil = (r0["shares_out"] / yo["shares_out"] - 1) * 100 if yo and r0.get("shares_out") and yo.get("shares_out") else None
    if any(abs(v) * 100 > SEC_MARGIN_MAX for v in mg.values()):
        # profit or loss several times revenue: the income statement is driven by asset revaluations (e.g. a bitcoin
        # treasury), not by the operating business, so margins say nothing about it → no score, axis stays unknown
        return {"score": None, "parts": parts, "period_end": r0["period_end"], "form": r0.get("form"), "dil": dil, "odd": True}
    for key, name in (("net_income", "net marj"), ("op_income", "faaliyet marjı")):
        if key in mg:
            parts.append((f"{name} ({lab})", _p(mg[key] * 100, 1, False), _clamp(30 + mg[key] * 250)))
    if len(parts) < 2:
        return {"score": None, "parts": parts, "period_end": r0["period_end"], "form": r0.get("form"), "dil": dil}
    return {"score": round(sum(x[2] for x in parts) / len(parts), 1), "parts": parts, "period_end": r0["period_end"], "form": r0.get("form"),
            "dil": dil}


# ── decide (pure) ────────────────────────────────────────

def _business(card: dict, src: str, sig: dict[str, list], quarters: list[dict] | None = None) -> tuple[str, list[dict], float | None, str | None]:
    """→ (axis state, red flags, temel score, score source 'karne'|'sec'|None). Appends signals to sig['for'|'against'|'notes'].
    The karne "temel" wins; only when it is missing are the stored SEC quarters used (never both)."""
    temel = _score(card, "temel")
    flags: list[dict] = []
    tsrc = "karne" if temel is not None else None
    sec = sec_temel(quarters) if temel is None else None
    if sec and sec["score"] is not None:
        temel, tsrc = sec["score"], "sec"
        src_s = f"SEC {sec.get('form') or 'çeyrek'} bildirimi, dönem sonu {sec['period_end']}"
        det = ", ".join(f"{lab} {val}" for lab, val, _ in sec["parts"])
        txt = f"SEC çeyrek bildirimlerinden temel eşdeğeri {tr_n(temel, 0)}/100 ({det}); karnede temel puan yoktu"
        if temel >= BUS_STRONG:
            sig["for"].append(_sig("temel_sec_guclu", txt + ".", 3, src_s))
        elif temel < BUS_WEAK:
            sig["against"].append(_sig("temel_sec_zayif", txt + ".", 3, src_s))
        else:
            sig["notes"].append(_sig("temel_sec_orta", txt + ": ne güçlü ne zayıf.", 1, src_s))
    elif temel is None and sec and sec.get("odd"):
        sig["notes"].append(_sig("temel_sec_anlamsiz", "Temel sağlamlık puanı yok: SEC bildirimlerinde kâr/zarar gelirin birkaç katı (varlık değerlemesi ağırlıklı "
                                                       "bir şirket), marjlar faaliyeti anlatmıyor; şirket ekseni bilinmiyor, bu yüzden 'Sağlam' denemez.", 2,
                                 f"SEC {sec.get('form') or 'çeyrek'} bildirimi, dönem sonu {sec['period_end']}"))
    elif temel is None:
        sig["notes"].append(_sig("temel_yok", "Temel sağlamlık puanı yok: karnede finansal veri alınamadı, yerel SEC çeyrek verisi de yetersiz; "
                                              "şirket ekseni bilinmiyor, bu yüzden 'Sağlam' denemez.", 2, src))
    else:
        g, nm = _part(card, "temel", "Gelir büyümesi"), _part(card, "temel", "Net marj")
        det = ", ".join(x for x in ((f"gelir büyümesi {g['value']}" if g and g.get("pts") is not None else ""),
                                    (f"net marj {nm['value']}" if nm and nm.get("pts") is not None else "")) if x)
        txt = f"Temel sağlamlık {tr_n(temel, 0)}/100" + (f" ({det})" if det else "")
        if temel >= BUS_STRONG:
            sig["for"].append(_sig("temel_guclu", txt + ".", 3, src))
        elif temel < BUS_WEAK:
            sig["against"].append(_sig("temel_zayif", txt + ".", 3, src))
        else:
            sig["notes"].append(_sig("temel_orta", txt + ": ne güçlü ne zayıf.", 1, src))
    neg = 0
    eps = _part(card, "haber", "2 çeyrek EPS revizyonu")
    eps_v = _pnum(eps["value"]) if eps else None
    if eps_v is not None and eps_v <= -REV_EPS:
        neg += 1
        sig["against"].append(_sig("eps_revizyon_asagi", f"Analistler 30 günde 2 çeyreklik EPS tahminini %{tr_n(-eps_v, 1)} indirdi.", 2, src))
    elif eps_v is not None and eps_v >= REV_EPS:
        sig["for"].append(_sig("eps_revizyon_yukari", f"Analistler 30 günde 2 çeyreklik EPS tahminini %{tr_n(eps_v, 1)} yükseltti.", 2, src))
    tg = _part(card, "haber", "Hedef fiyat eğilimi")
    tg_v = _pnum(tg["value"]) if tg else None
    if tg_v is not None and tg_v <= -REV_TARGET:
        neg += 1
        sig["against"].append(_sig("hedef_asagi", f"Analist hedef fiyat ortalaması son ayda çeyrek ortalamasından %{tr_n(-tg_v, 1)} düşük.", 1, src))
    elif tg_v is not None and tg_v >= REV_TARGET:
        sig["for"].append(_sig("hedef_yukari", f"Analist hedef fiyat ortalaması son ayda çeyrek ortalamasından %{tr_n(tg_v, 1)} yüksek.", 1, src))
    gr = _part(card, "haber", "Not değişiklikleri")
    if gr:
        m = re.findall(r"\d+", str(gr.get("value", "")))
        ups, downs = (int(m[0]), int(m[1])) if len(m) >= 2 else (0, 0)
        if downs >= GRADES_MIN and downs >= 2 * ups:
            neg += 1
            sig["against"].append(_sig("not_indirimi", f"30 günde {downs} not indirimi, {ups} yükseltme.", 2, src))
        elif ups >= GRADES_MIN and ups >= 2 * downs:
            sig["for"].append(_sig("not_yukseltme", f"30 günde {ups} not yükseltmesi, {downs} indirme.", 1, src))
    dil = _part(card, "anomali", "Hisse sayısı artışı")
    if dil:
        neg += 1
        sig["against"].append(_sig("sulandirma", f"Hisse sayısı bir yılda {dil['value']} arttı (sulandırma).", 2, src))
        flags.append({"key": "sulandirma", "clear": "Sonraki çeyrek verisinde yıllık hisse sayısı artışı %3 sınırının altına inerse"})
    ins = _part(card, "anomali", "Yoğun içeriden satış")
    if ins:
        sig["against"].append(_sig("iceriden_satis", f"Son 90 günde yoğun içeriden satış ({ins['value']}).", 1, src))
        flags.append({"key": "iceriden_satis", "clear": "90 günlük pencerede yoğun içeriden satış kalmazsa"})
    if _part(card, "anomali", "İçeriden piyasa alımı"):
        sig["for"].append(_sig("iceriden_alim", f"Son 12 ayda içeriden piyasa alımı var ({_part(card, 'anomali', 'İçeriden piyasa alımı')['value']}).", 1, src))
    if _part(card, "anomali", "Geri alım"):
        sig["for"].append(_sig("geri_alim", f"Hisse sayısı azalıyor (geri alım, {_part(card, 'anomali', 'Geri alım')['value']}).", 1, src))
    if tsrc != "karne" and sec and sec.get("dil") is not None and not dil and not _part(card, "anomali", "Geri alım"):
        src_s = f"SEC {sec.get('form') or 'çeyrek'} bildirimi, dönem sonu {sec['period_end']}"
        if sec["dil"] > DILUTION:
            neg += 1
            sig["against"].append(_sig("sulandirma", f"SEC verisine göre hisse sayısı bir yılda {_p(sec['dil'])} arttı (sulandırma).", 2, src_s))
            flags.append({"key": "sulandirma", "clear": "Sonraki çeyrek verisinde yıllık hisse sayısı artışı %3 sınırının altına inerse"})
        elif sec["dil"] < -1:
            sig["for"].append(_sig("geri_alim", f"SEC verisine göre hisse sayısı bir yılda {_p(sec['dil'])} azaldı (geri alım).", 1, src_s))
    fv = card.get("fair") or {}
    if fv.get("fair") is not None and fv.get("upside") is not None:
        if fv.get("confidence") == "düşük":
            sig["notes"].append(_sig("degerleme_disarida", "Değerleme hesaba katılmadı: adil değer yöntemleri birbirinden çok uzak (güven düşük).", 1, src))
        elif fv["upside"] >= VAL_EDGE:
            sig["for"].append(_sig("degerleme_ucuz", f"Fiyat adil değer tahmininin ({_usd(fv['fair'])}) altında: potansiyel {_p(fv['upside'])}, güven {fv['confidence']}.", 1, src))
        elif fv["upside"] <= -VAL_EDGE:
            sig["against"].append(_sig("degerleme_pahali", f"Fiyat adil değer tahmininin ({_usd(fv['fair'])}) üstünde: {_p(fv['upside'])}, güven {fv['confidence']}.", 1, src))
    if temel is None:
        state = "bilinmiyor"
    else:
        state = "guclu" if temel >= BUS_STRONG else "zayif" if temel < BUS_WEAK else "orta"
    if eps_v is not None and eps_v <= -REV_EPS and neg >= BIZ_NEG_STEP and state in ("guclu", "orta"):
        state = {"guclu": "orta", "orta": "zayif"}[state]
        sig["notes"].append(_sig("tahmin_asinmasi", "Tahmin indirimleri ve diğer olumsuz analist işaretleri birlikte: şirket ekseni bir kademe düşük okundu.", 1, src))
    return state, flags, temel, tsrc


def _mas(t: dict) -> str:
    return "50 ve 200 günlük ortalamaların" if t.get("sma200") is not None else "50 günlük ortalamanın"


def _price(t: dict, src: str, sig: dict[str, list]) -> str:
    px, s50, s200 = t.get("price"), t.get("sma50"), t.get("sma200")
    a50, a200 = bool(t.get("above50")), bool(t.get("above200"))
    rs = t.get("rs63_vs_spy")
    if s200 is None:
        broken = not a50 and (rs is None or rs <= RS_WEAK / 2)
        strong = a50 and (rs is None or rs >= 0)
    else:
        broken = not a50 and not a200
        strong = a50 and a200 and (rs is None or rs >= 0)
    ts = t.get("trend_score")
    lv = f"50 günlük {_usd(s50)}" + (f", 200 günlük {_usd(s200)}" if s200 is not None else "; 200 günlük için geçmiş yetersiz")
    if broken:
        sig["against"].append(_sig("fiyat_bozuk", f"Fiyat ({_usd(px)}) {_mas(t)} altında ({lv}); trend puanı {tr_n(ts, 0)}/100.", 3, src))
    elif strong:
        sig["for"].append(_sig("fiyat_guclu", f"Fiyat ({_usd(px)}) {_mas(t)} üstünde ({lv}); trend puanı {tr_n(ts, 0)}/100.", 2, src))
    else:
        sig["notes"].append(_sig("fiyat_orta", f"Grafik kararsız: {t.get('trend_label') or 'karışık'} ({lv}, fiyat {_usd(px)}).", 1, src))
    if rs is not None and rs <= RS_WEAK:
        sig["against"].append(_sig("rs_zayif", f"3 ayda S&P 500'ün {tr_n(-rs, 1)} puan gerisinde.", 1, src))
    elif rs is not None and rs >= RS_STRONG:
        sig["for"].append(_sig("rs_guclu", f"3 ayda S&P 500'ü {tr_n(rs, 1)} puan geçti.", 1, src))
    fh = t.get("from_hi52")
    if fh is not None and fh <= FROM_HI_DEEP:
        sig["against"].append(_sig("zirveden_uzak", f"52 haftalık zirvenin ({_usd(t.get('hi52'))}) %{tr_n(-fh, 0)} altında.", 1, src))
    r = t.get("rsi14")
    if r is not None and r < 30:
        sig["notes"].append(_sig("rsi_asiri_satim", f"RSI {tr_n(r, 0)}: kısa vadede aşırı satım bölgesi (yön bilgisi değil, durum).", 1, src))
    elif r is not None and r > 75:
        sig["notes"].append(_sig("rsi_asiri_alim", f"RSI {tr_n(r, 0)}: kısa vadede aşırı alım bölgesi.", 1, src))
    return "bozuk" if broken else "guclu" if strong else "orta"


def decide(facts: dict) -> dict:
    """PURE: facts (from gather) → verdict card. No DB, no clock, no network."""
    sym, today = facts["symbol"], facts["today"]
    t, card, pos, th = facts.get("tech"), facts.get("card"), facts.get("position"), facts.get("thesis")
    reg = facts.get("regime")
    src_c = f"karne {card.get('as_of') or '—'}" if card else "karne yok"
    src_t = f"Yahoo kapanış {t.get('as_of')}" if t else "fiyat yok"
    out = {"symbol": sym, "as_of": (t or {}).get("as_of"), "price": (t or {}).get("price"), "rules_ver": RULES_VER,
           "for": [], "against": [], "notes": [], "changers": {"up": [], "down": []}, "next": [], "todo": [],
           "position": None, "changed": None, "comment": None, "comment_model": None, "sources": []}
    if pos:
        out["position"] = {k: pos.get(k) for k in ("weight", "unreal_pct", "value", "risk_share", "beta")}
    if not card or not t:
        miss = " ve ".join(x for x in (("karne" if not card else ""), ("fiyat geçmişi" if not t else "")) if x)
        out.update(_stance("veri_yok"), pattern="veri_yok", pattern_tr="Veri eksik", reason="veri eksik",
                   headline=f"{sym}: {miss} olmadığı için karar üretilmedi.")
        out["changers"]["up"].append({"text": f"Gece karnesi ve fiyat verisi oluşunca kart yeniden hesaplanır ({miss} eksik)."})
        out["sources"] = [s for s in (src_c if card else None, src_t if t else None) if s]
        return out
    sig = {"for": [], "against": [], "notes": []}
    flags: list[dict] = []

    biz, bflags, temel, tsrc = _business(card, src_c, sig, facts.get("sec"))
    out["business"] = {"state": biz, "score": temel, "src": tsrc}
    tl = "Temel (SEC) eşdeğer" if tsrc == "sec" else "Temel sağlamlık"
    flags += bflags
    price = _price(t, src_t, sig)
    pattern, pattern_tr = PATTERNS[(biz, price)]

    # thesis
    src_th = f"tez defteri {today}"
    rules = (th or {}).get("rules") or []
    exits = [r for r in rules if r["kind"] in EXIT_KINDS and not r.get("good")]
    broken_rules = [r for r in rules if r.get("status") == "triggered" and not r.get("good")]
    if th:
        tid = th["id"]
        for r in broken_rules:
            sig["against"].append(_sig("kural_ihlali", f"Kendi çıkış kuralın tetiklendi: {r['label']}" + (f" (şu an {r['last_value']})." if r.get("last_value") else "."), 3,
                                       f"tez defteri #{tid}"))
            out["todo"].append({"text": f"Tetiklenen kuralı gözden geçir: {r['label']}", "href": f"/tez#t{tid}"})
        for r in rules:
            if r.get("status") == "triggered" and r.get("good"):
                sig["notes"].append(_sig("hedef_kurali", f"Hedef kuralın gerçekleşti: {r['label']}.", 2, f"tez defteri #{tid}"))
        if th.get("review_due"):
            sig["against"].append(_sig("tez_gozden_gecirme", f"Tezin gözden geçirme tarihi ({th.get('next_review')}) geldi.", 1, f"tez defteri #{tid}"))
            flags.append({"key": "tez_gozden_gecirme", "clear": "Tez gözden geçirilip sonraki tarih işlenirse"})
            out["todo"].append({"text": "Tezi gözden geçir ve sonucu deftere yaz", "href": f"/tez#t{tid}"})
        if not exits:
            sig["notes"].append(_sig("cikis_kurali_yok", "Tez var ama ölçülebilir bir çıkış kuralı yok.", 2, f"tez defteri #{tid}"))
            out["todo"].append({"text": "Teze ölçülebilir bir çıkış kuralı ekle (fiyat, zarar veya karne sınırı)", "href": f"/tez#t{tid}"})
        elif not broken_rules:
            sig["for"].append(_sig("tez_saglam", f"Yazılı tez ve {len(exits)} çıkış kuralı var; hiçbiri tetiklenmedi.", 1, f"tez defteri #{tid}"))
    elif pos:
        sig["notes"].append(_sig("tez_yok", "Bu pozisyon için yazılı tez ve çıkış kuralı yok.", 2, src_th))
        out["todo"].append({"text": f"{sym} için tezini ve çıkış kuralını yaz", "href": f"/tez?symbol={sym}#yeni"})

    # position
    deep = False
    src_p = f"radar defteri {facts.get('position_as_of') or out['as_of']}"
    if pos:
        u = pos.get("unreal_pct")
        if u is not None and u <= LOSS_DEEP:
            deep = True
            sig["against"].append(_sig("derin_zarar", f"Pozisyon maliyetin %{tr_n(-u, 1)} altında (maliyet {_usd(pos.get('avg'))}).", 2, src_p))
        elif u is not None and u >= GAIN_BIG:
            sig["notes"].append(_sig("buyuk_kar", f"Pozisyon maliyetin %{tr_n(u, 1)} üstünde.", 1, src_p))
        wgt = pos.get("weight")
        if wgt is not None and wgt > WEIGHT_HIGH:
            sig["against"].append(_sig("agirlik_yuksek", f"Portföy ağırlığı %{tr_n(wgt, 1)} (eşik %{tr_n(WEIGHT_HIGH, 0)}).", 1, src_p))
        rsh = pos.get("risk_share")
        if rsh is not None and wgt and wgt >= 3 and rsh >= RISK_SHARE_X * wgt:
            sig["against"].append(_sig("risk_payi_yuksek", f"Portföy riskindeki payı %{tr_n(rsh, 1)}, ağırlığı ise %{tr_n(wgt, 1)}.", 1, src_p))
        cl = pos.get("cluster")
        if cl and cl.get("weight", 0) > CLUSTER_HIGH:
            sig["notes"].append(_sig("kume", f"Birlikte hareket eden kümenin parçası: {', '.join(cl['symbols'][:5])} (küme %{tr_n(cl['weight'], 1)}, "
                                             f"ort. korelasyon {tr_n(cl.get('avg_corr'), 2)}).", 1, src_p))
    deep_noexit = deep and not exits

    # news: distinct stories
    nw = facts.get("news") or {}
    tone = news_tone(nw.get("events") or [])
    src_n = f"radar haber akışı {NEWS_DAYS} gün, {today}"
    if tone["n"] >= NEWS_MIN_STORIES and tone["net"] is not None:
        top = next((s for s in tone["stories"] if (s["dir"] < 0) == (tone["net"] < 0) and s["dir"] != 0), tone["stories"][0])
        cnt = (f"{NEWS_DAYS} günde {tone['n']} ayrı haber hikâyesi (önem ≥{NEWS_MIN_IMP}, {tone['events']} kayıt)" if tone["by_story"] else
               f"{NEWS_DAYS} günde önem ≥{NEWS_MIN_IMP} olan {tone['events']} haber kaydı, başlık benzerliğiyle {tone['n']} grup "
               f"(hikâye anahtarı yok; gerçek hikâye sayısı daha az olabilir)")
        base = (f"{cnt}; grup başına tek oy, net ton {tr_n(tone['net'], 2, True)}"
                f"; en önemlisi: {str(top['title'] or '')[:110]}")
        if tone["net"] <= -NEWS_TONE:
            sig["against"].append(_sig("haber_negatif", base + ".", 2, src_n))
            flags.append({"key": "haber_negatif", "clear": f"{NEWS_DAYS} günlük haber tonu (hikâye/grup başına tek oy) {tr_n(-NEWS_TONE, 2)} üstüne dönerse"})
        elif tone["net"] >= NEWS_TONE:
            sig["for"].append(_sig("haber_pozitif", base + ".", 1, src_n))
        else:
            sig["notes"].append(_sig("haber_notr", base + ".", 1, src_n))
    elif tone["n"]:
        sig["notes"].append(_sig("haber_az", f"{NEWS_DAYS} günde yalnızca {tone['n']} " + ("ayrı önemli haber hikâyesi" if tone["by_story"] else f"önemli haber grubu ({tone['events']} kayıt)")
                                         + " var; ton değerlendirilmedi.", 1, src_n))

    # market regime vs this stock's beta
    beta = (pos or {}).get("beta")
    if reg and reg.get("score") is not None and beta is not None and beta >= BETA_HIGH:
        src_r = f"radar rejim {reg.get('day')}"
        if reg["score"] < REGIME_RISKOFF:
            sig["against"].append(_sig("rejim_beta", f"Piyasa {reg['label']} ({tr_n(reg['score'], 0)}/100) ve hissenin betası {tr_n(beta, 2)}: "
                                                      "piyasa düşüşlerinden ortalamadan sert etkilenir.", 2, src_r))
            flags.append({"key": "rejim_beta", "clear": f"Piyasa rejimi {REGIME_RISKOFF} üstüne dönerse"})
        elif reg["score"] >= REGIME_BULL:
            sig["notes"].append(_sig("rejim_beta", f"Piyasa {reg['label']} ({tr_n(reg['score'], 0)}/100); beta {tr_n(beta, 2)}: rüzgâr şimdilik arkada, "
                                                    "rejim bozulursa ilk etkilenenlerden olur.", 1, src_r))

    # catalysts
    for e in facts.get("earnings") or []:
        d = _days(today, e["day"])
        if 0 <= d <= NEXT_DAYS:
            out["next"].append({"day": e["day"], "text": e["name"]})
        if 0 <= d <= EARN_SOON:
            sig["notes"].append(_sig("bilanco_yakin", f"Bilanço {e['day']} ({d} gün sonra): fiyat sert oynayabilir; karar bilançodan sonra yeniden hesaplanır.", 2,
                                     "bilanço takvimi"))
            out["todo"].append({"text": "Bilançodan önce tezdeki beklentiyi not et", "href": f"/tez?symbol={sym}#yeni" if not th else f"/tez#t{th['id']}"})
    if th and th.get("next_review") and 0 <= _days(today, th["next_review"]) <= NEXT_DAYS:
        out["next"].append({"day": th["next_review"], "text": "Tez gözden geçirme tarihi"})
    for r in rules:
        if r["kind"] == "deadline" and r.get("status") != "triggered":
            try:
                if 0 <= _days(today, r["param"]) <= NEXT_DAYS:
                    out["next"].append({"day": r["param"], "text": f"Tez kuralı son tarihi: {r['label']}"})
            except ValueError:
                pass
    out["next"].sort(key=lambda x: x["day"])

    # ── stance ladder ──
    # an undecided chart that also lags the index badly or sits far below its high is not "sound", whatever the business
    price_weak = any(x["key"] in ("rs_zayif", "zirveden_uzak") for x in sig["against"])
    if broken_rules:
        stance, why = "alarm", "kural"
        reason = f"çıkış kuralı tetiklendi ({broken_rules[0]['label']})"
    elif price == "bozuk" and (biz == "zayif" or deep_noexit):
        stance, why = "sorgula", "ikisi" if biz == "zayif" else "zarar"
        reason = "şirket ve grafik birlikte zayıf" if why == "ikisi" else \
            f"grafik bozuk, zarar %{tr_n(-(pos.get('unreal_pct') or 0), 1)}, çıkış kuralı yok"
    elif biz in ("guclu", "orta") and (price == "guclu" or (price == "orta" and not price_weak)) and not flags:
        stance, why, reason = "saglam", "", "kırmızı bayrak yok"
    else:
        stance, why = "izle", ""
        reason = _lc(pattern_tr) if price == "bozuk" or biz in ("zayif", "bilinmiyor") or not flags else FLAG_TR.get(flags[0]["key"], flags[0]["key"])
    out["reason"] = reason
    for k in ("for", "against", "notes"):
        out[k] = sorted(sig[k], key=lambda s: -s["w"])
    out.update(_stance(stance), pattern=pattern, pattern_tr=pattern_tr)
    out["changers"] = _changers(stance, biz, price, temel, t, pos, exits, broken_rules, flags, deep, reg, beta, tl)
    if stance == "izle" and price == "orta" and price_weak and biz in ("guclu", "orta") and not flags:
        out["changers"]["up"].insert(0, {"text": f"Fiyat {_mas(t)} üstüne çıkarsa; ya da 3 aylık S&P farkı {tr_n(RS_WEAK, 0)} puanın üstüne ve 52 haftalık "
                                                 f"zirveye uzaklık %{tr_n(-FROM_HI_DEEP, 0)}'in altına dönerse"})
    out["headline"] = _headline(sym, stance, why, pattern_tr, temel, t, pos, broken_rules, out["against"], tsrc)
    seen: dict[str, dict] = {}
    for td in out["todo"]:
        if td["href"] in seen:
            seen[td["href"]]["text"] += " (bilançodan önce)" if "Bilanço" in td["text"] else ""
        else:
            seen[td["href"]] = td
    out["todo"] = list(seen.values())
    out["sources"] = [s for s in (f"Karne ({card.get('as_of')}, hesaplandı {str(card.get('created_at') or '')[:10]})", f"Yahoo günlük kapanış ({t.get('as_of')})",
                                  f"Radar defteri ({src_p.split()[-1]})" if pos else None, f"Tez defteri ({today})",
                                  f"Radar haber akışı ({NEWS_DAYS} gün, {tone['events']} kayıt / {tone['n']} " + ("hikâye" if tone["by_story"] else "grup") + f", {today})",
                                  f"Radar rejim ({reg.get('day')})" if reg else None,
                                  f"Portföy riski ({facts.get('risk_as_of')})" if facts.get("risk_as_of") else None,
                                  "Bilanço takvimi (Finnhub)" if facts.get("earnings") else None) if s]
    return out


def _stance(key: str) -> dict:
    label, tone, rank = STANCES[key]
    return {"stance": key, "stance_tr": label, "tone": tone, "rank": rank}


def _reclaim(t: dict) -> str:
    """The nearer average above the price: closing above it ends 'broken'."""
    lv = [(n, v) for n, v in (("50", t.get("sma50")), ("200", t.get("sma200"))) if v is not None]
    n, v = min(lv, key=lambda x: x[1]) if lv else ("50", None)
    return f"Kapanış {n} günlük ortalamanın ({_usd(v)}) üstüne çıkarsa fiyat ekseni 'bozuk' olmaktan çıkar"


def _break(t: dict) -> str:
    if t.get("sma200") is None:
        return f"Kapanış 50 günlük ortalamanın ({_usd(t.get('sma50'))}) altına inerse"
    return f"Kapanış hem 50 günlük ({_usd(t.get('sma50'))}) hem 200 günlük ({_usd(t.get('sma200'))}) ortalamanın altına inerse"


def _changers(stance, biz, price, temel, t, pos, exits, broken_rules, flags, deep, reg, beta, tl="Temel sağlamlık") -> dict:
    up, down = [], []
    loss_px = pos["avg"] * (1 + LOSS_DEEP / 100) if pos and pos.get("avg") else None
    loss_txt = f"fiyat {_usd(loss_px)} altına inerse (maliyetin %{tr_n(-LOSS_DEEP, 0)} altı)" if loss_px else None
    rule_down = [{"text": f"Kendi çıkış kuralın tetiklenirse: {r['label']}"} for r in exits if r.get("status") != "triggered"][:2]
    if stance == "alarm":
        up.append({"text": "Tetiklenen kural (" + "; ".join(r["label"] for r in broken_rules) + ") gözden geçirilip tez güncellenirse kart yeniden hesaplanır"})
    elif stance == "sorgula":
        up.append({"text": _reclaim(t)})
        if biz == "zayif" and not (deep and not exits):
            up.append({"text": f"{tl} puanı {BUS_WEAK} üstüne çıkarsa (şu an {tr_n(temel, 0)})" if temel is not None and temel < BUS_WEAK
                       else f"Tahmin indirimleri durursa (30 günlük EPS revizyonu -%{tr_n(REV_EPS, 0)} sınırının üstüne dönerse)"})
        elif biz != "zayif":
            up.append({"text": "Tez yazılıp ölçülebilir bir çıkış kuralı konursa"})
            if loss_px:
                up.append({"text": f"Fiyat {_usd(loss_px)} üstüne çıkarsa (zarar %{tr_n(-LOSS_DEEP, 0)} sınırının içine döner)"})
        else:
            up.append({"text": f"{tl} puanı {BUS_WEAK} üstüne çıkar ve tez + çıkış kuralı yazılırsa"})
        down += rule_down
    elif stance == "izle":
        if price == "bozuk":
            up.append({"text": _reclaim(t)})
        if biz == "zayif":
            up.append({"text": f"{tl} puanı {BUS_WEAK} üstüne çıkarsa (şu an {tr_n(temel, 0)})" if temel is not None and temel < BUS_WEAK
                       else f"Tahmin indirimleri durursa (30 günlük EPS revizyonu -%{tr_n(REV_EPS, 0)} sınırının üstüne dönerse)"})
        if biz == "bilinmiyor":
            up.append({"text": "Gece karnesinde temel sağlamlık puanı hesaplanırsa (finansal veri gelirse) şirket ekseni okunur"})
        up += [{"text": f["clear"]} for f in flags]
        if price != "bozuk":
            if biz == "zayif" or (deep and not exits):
                down.append({"text": _break(t)})
            else:
                down.append({"text": _break(t) + f" ve {tl.lower()} puanı {BUS_WEAK} altına düşerse"})
                if loss_txt and not exits:
                    down.append({"text": _break(t) + " ve " + loss_txt + ", çıkış kuralı yokken"})
        else:
            if biz != "zayif":
                down.append({"text": f"{tl} puanı {BUS_WEAK} altına düşerse (şu an {tr_n(temel, 0)})" if temel is not None
                             else "Temel puan zayıf banda düşerse"})
            if loss_txt and not exits and not deep:
                down.append({"text": loss_txt[0].upper() + loss_txt[1:] + " ve çıkış kuralı yazılmamışsa"})
        down += rule_down
    elif stance == "saglam":
        down.append({"text": _break(t)})
        if temel is not None:
            down.append({"text": f"{tl} puanı {BUS_WEAK} altına düşerse (şu an {tr_n(temel, 0)})"})
        down.append({"text": f"{NEWS_DAYS} günlük haber tonu (hikâye/grup başına tek oy) {tr_n(-NEWS_TONE, 2)} altına inerse (en az {NEWS_MIN_STORIES} hikâye/grup)"})
        if beta is not None and beta >= BETA_HIGH and reg and reg.get("score") is not None:
            down.append({"text": f"Piyasa rejimi {REGIME_RISKOFF} altına inerse (beta {tr_n(beta, 2)}; şu an {tr_n(reg['score'], 0)})"})
        down += rule_down
    return {"up": up, "down": down}


def _lc(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _headline(sym, stance, why, pattern_tr, temel, t, pos, broken_rules, against, tsrc=None) -> str:
    tm = (f"temel (SEC) {tr_n(temel, 0)}/100" if tsrc == "sec" else f"temel {tr_n(temel, 0)}/100") if temel is not None else "temel puan yok"
    ma = _mas(t) + " altında"
    if stance == "alarm":
        r = broken_rules[0]
        return f"{sym}: kendi çıkış kuralın tetiklendi ({r['label']}); tezin hâlâ geçerli mi, defterde yanıtla."
    if stance == "sorgula" and why == "ikisi":
        return f"{sym}: hem şirket tarafı zayıf ({tm}) hem fiyat {ma}; tezi sorgulama zamanı."
    if stance == "sorgula":
        return (f"{sym}: fiyat {ma}, pozisyon maliyetin %{tr_n(-(pos.get('unreal_pct') or 0), 1)} altında ve yazılı çıkış kuralı yok; "
                "tezi sorgulama zamanı.")
    if stance == "saglam":
        return f"{sym}: {_lc(pattern_tr)} ({tm}, trend {tr_n(t.get('trend_score'), 0)}/100); kırmızı bayrak yok."
    main = next((s["text"] for s in against if s["key"] not in ("fiyat_bozuk", "temel_zayif", "temel_sec_zayif")), None)
    return f"{sym}: {_lc(pattern_tr)} ({tm}, trend {tr_n(t.get('trend_score'), 0)}/100)" + (f"; en önemli karşı işaret: {_lc(main.rstrip('.'))}." if main else ".")


# ── board (pure) ─────────────────────────────────────────

def build_board(cards: list[dict], p: dict) -> dict:
    """Portfolio-level conclusion from the cards plus portfolio facts p
    {as_of, regime, regime_prev, risk: {beta, stock_pct, ceiling, warnings, as_of}, recent_loss, total}."""
    cards = sorted(cards, key=lambda c: (-c["rank"], -((c.get("position") or {}).get("weight") or 0)))
    counts = []
    for k, (label, tone, rank) in sorted(STANCES.items(), key=lambda kv: -kv[1][2]):
        cs = [c for c in cards if c["stance"] == k]
        if cs:
            counts.append({"stance": k, "stance_tr": label, "tone": tone, "n": len(cs),
                           "weight_pct": round(sum((c.get("position") or {}).get("weight") or 0 for c in cs), 1), "symbols": [c["symbol"] for c in cs]})
    W = lambda c: (c.get("position") or {}).get("weight") or 0  # noqa: E731
    agenda = []
    for c in [c for c in cards if c["stance"] == "alarm"]:
        td = next((x for x in c["todo"] if x["href"].startswith("/tez#")), None)
        agenda.append({"text": f"{c['symbol']} (%{tr_n(W(c), 1)}): {c.get('reason') or 'çıkış kuralı tetiklendi'}; tezi gözden geçir.", "symbol": c["symbol"],
                       "href": td["href"] if td else "/tez", "w": 3, "_s": 1000 + W(c)})
    q = [c for c in cards if c["stance"] == "sorgula"]
    for c in q[:AGENDA_SORGULA]:
        agenda.append({"text": f"{c['symbol']} (%{tr_n(W(c), 1)}): tezi sorgula; {c.get('reason') or c['pattern_tr'].lower()}.", "symbol": c["symbol"],
                       "href": f"/h/{c['symbol']}#karar", "w": 2, "_s": 100 + W(c)})
    if q[AGENDA_SORGULA:]:
        rest = q[AGENDA_SORGULA:]
        agenda.append({"text": f"Tezi sorgulanacak diğerleri: {', '.join(c['symbol'] for c in rest)} (toplam ağırlık %{tr_n(sum(W(c) for c in rest), 1)}).",
                       "symbol": None, "href": "/karar", "w": 2, "_s": 100})
    missing = [c for c in cards if any(s["key"] == "tez_yok" for s in c["notes"])]
    if missing:
        mw = sum(W(c) for c in missing)
        agenda.append({"text": f"{len(missing)} pozisyonun yazılı tezi ve çıkış kuralı yok (toplam ağırlık %{tr_n(mw, 0)}): "
                               + ", ".join(c["symbol"] for c in missing[:10]) + ".", "symbol": None, "href": "/tez", "w": 2, "_s": 50 + mw / 10})
    rk = p.get("risk") or {}
    for w_ in [x for x in rk.get("warnings") or [] if x.get("key") in ("cluster", "ceiling", "single", "beta", "dd")][:2]:
        agenda.append({"text": w_["text"], "symbol": None, "href": "/portfolio/risk", "w": 1, "_s": 20})
    rl = p.get("recent_loss")
    if rl:
        agenda.append({"text": rl["text"], "symbol": None, "href": "/portfolio/davranis", "w": 1, "_s": 10})
    agenda.sort(key=lambda a: -a["_s"])
    agenda = [{k: v for k, v in a.items() if k != "_s"} for a in agenda[:5]]

    reg = p.get("regime")
    market = None
    if reg:
        prev = p.get("regime_prev")
        parts = [f"Piyasa {reg['label']} ({tr_n(reg['score'], 0)}/100"
                 + (f"; {prev['day']} tarihinde {tr_n(prev['score'], 0)}, {'yükseliyor' if reg['score'] > prev['score'] + 2 else 'düşüyor' if reg['score'] < prev['score'] - 2 else 'yatay'}" if prev else "")
                 + ")."]
        if rk.get("beta") is not None:
            parts.append(f"Portföy betası {tr_n(rk['beta'], 2)}: S&P %1 oynadığında portföy ortalama %{tr_n(rk['beta'], 1)} oynar.")
        if rk.get("stock_pct") is not None and rk.get("ceiling") is not None:
            over = rk["stock_pct"] > rk["ceiling"] + 5
            parts.append(f"Hisse oranı %{tr_n(rk['stock_pct'], 0)}, bu rejimde kural tavanı %{rk['ceiling']}"
                         + (" (tavanın üstünde; yeni alımlar için ölçü)." if over else "."))
        wk = reg.get("weakest")
        if wk:
            parts.append(f"En zayıf ayak {wk['name'].lower()} ({tr_n(wk['score'], 0)}): {wk['trigger']}.")
        market = {"label": reg["label"], "score": reg["score"], "tone": reg.get("tone") or "neu", "text": " ".join(parts)}

    held = [c for c in cards if c.get("position")]
    tot = sum(W(c) for c in held)
    seg = "; ".join(f"{x['stance_tr']} %{tr_n(x['weight_pct'], 0)} ({x['n']} hisse)" for x in counts if x["weight_pct"] > 0)
    # the market has its own block (board["market"]["text"]); the headline is about the holdings
    head = f"Hisse pozisyonları (portföy ağırlığı %{tr_n(tot, 0)}): {seg}" if seg else f"{len(cards)} hisse için karar hesaplandı"
    head += f". Öncelik: {_lc(agenda[0]['text'].rstrip('.'))}." if agenda else "."
    return {"as_of": p.get("as_of") or max((c["as_of"] for c in cards if c.get("as_of")), default=None), "headline": head, "market": market,
            "stance_counts": counts, "agenda": agenda,
            "changes": [{"symbol": c["symbol"], "from_tr": c["changed"]["from_tr"], "to_tr": c["stance_tr"], "day": c["changed"]["day"]}
                        for c in cards if c.get("changed")],
            "cards": cards, "comment": None, "comment_model": None, "run": None}


# ── gather (stored data only) ────────────────────────────

def context() -> dict:
    """Shared per-run cache: positions by symbol (stocks, summed over sub-portfolios), risk, regime, earnings rows."""
    from . import portfolio, regime
    ctx: dict = {"today": datetime.now(config.NY_TZ).date().isoformat()}
    led = portfolio.build()
    agg: dict[str, dict] = {}
    for p in portfolio.positions(led=led):
        if p["asset"] != "stock":
            continue
        a = agg.setdefault(p["symbol"], {"symbol": p["symbol"], "value": 0.0, "cost": 0.0, "qty": 0.0, "weight": 0.0, "price_day": p["price_day"]})
        a["value"] += p["value"]
        a["cost"] += p["cost"]
        a["qty"] += p["qty"] * p["multiplier"]
        a["weight"] += p["weight"] or 0
    for a in agg.values():
        a["avg"] = a["cost"] / a["qty"] if a["qty"] else None
        a["unreal_pct"] = (a["value"] / a["cost"] - 1) * 100 if a["cost"] > 0 else None
    ctx["positions"] = agg
    ctx["position_as_of"] = led.days[-1] if led.days else None
    try:
        ctx["risk"] = _risk_offline(led)
    except Exception:  # noqa: BLE001 - a missing risk block must not stop the verdicts
        log.exception("karar: risk failed")
        ctx["risk"] = None
    r = regime.latest()
    ctx["regime_row"] = r
    ctx["led"] = led
    now = utcnow()
    ctx["earnings"] = [dict(x) for x in db.query("SELECT key, name, release_at FROM macro_calendar WHERE key LIKE 'earnings:%' AND release_at >= ? AND release_at <= ? "
                                                  "ORDER BY release_at", (iso(now - timedelta(hours=12)), iso(now + timedelta(days=NEXT_DAYS + 1))))]
    return ctx


def _cached_sector(sym: str) -> tuple[str, str]:
    """Sector from the in-process map or the API cache only (no FMP/Yahoo call); uncached → 'Bilinmiyor' (unused here)."""
    from . import risk
    if sym in risk._SECTORS:
        return risk._SECTORS[sym]
    r = db.query_one("SELECT body FROM api_cache WHERE key = ?", (f"fmp:profile?symbol={sym}",))
    p = db.loads(r["body"], None) if r else None
    p = p[0] if isinstance(p, list) and p else p
    return (p.get("sector") or "Bilinmiyor", p.get("industry") or "—") if isinstance(p, dict) else ("Bilinmiyor", "—")


def _risk_offline(led):
    """risk._compute with a cache-only sector lookup (risk.compute would call FMP for uncached profiles; the free FMP quota
    is the scarce resource). Uncached, so risk's own result cache is untouched; no module globals are swapped."""
    from . import risk
    return risk._compute(led, sector=_cached_sector)


def _regime_brief(r: dict | None) -> dict | None:
    if not r:
        return None
    comps = [c for c in (r.get("comps") or {}).values() if isinstance(c, dict) and c.get("score") is not None]
    wk = min(comps, key=lambda c: c["score"]) if comps else None
    return {"day": r["day"], "label": r["label"], "score": r["overall"], "tone": r.get("tone"),
            "weakest": {"name": wk["name"], "score": wk["score"], "trigger": wk.get("trigger") or ""} if wk else None}


def _news_events(sym: str, today: str) -> list[dict]:
    since = (date.fromisoformat(today) - timedelta(days=NEWS_DAYS)).isoformat()
    rows = db.query("""SELECT e.id, e.title, e.title_key, e.published_at, COALESCE(a.importance, e.importance) imp,
                              COALESCE(a.direction, e.direction, 0) d, a.story, a.portfolio_impact
                       FROM events e LEFT JOIN analyses a ON a.event_id = e.id
                       WHERE e.published_at >= ? AND e.type IN ('news','filing','rating')
                         AND e.status NOT IN ('duplicate','irrelevant','backfill') AND COALESCE(a.importance, e.importance, 0) >= ?
                         AND (e.symbol = ? OR EXISTS (SELECT 1 FROM event_symbols s WHERE s.event_id = e.id AND s.symbol = ?))""",
                    (since, NEWS_MIN_IMP, sym, sym))
    out = []
    for r in rows:
        d = r["d"] or 0
        imp_map = db.loads(r["portfolio_impact"], None)
        if isinstance(imp_map, dict) and imp_map.get(sym) is not None:
            try:
                d = int(imp_map[sym])
            except (TypeError, ValueError):
                pass
        out.append({"story": f"s:{r['story']}" if r["story"] else None, "tk": r["title_key"], "dir": d, "imp": r["imp"], "title": r["title"],
                    "at": r["published_at"][:10]})
    return out


def gather(symbol: str, ctx: dict | None = None) -> dict:
    """Raw facts for one symbol from stored data only (no hub.fetch / scorecard.compute / paid API)."""
    from . import scorecard, technicals, thesis
    sym = symbol.upper().strip()
    ctx = ctx if ctx is not None else context()
    today = ctx["today"]
    c = scorecard.latest(sym)
    card = None
    if c:
        card = {"as_of": c.get("as_of"), "created_at": c.get("created_at"), "overall": c.get("overall"),
                "dims": {d["key"]: {"score": d["score"], "parts": d["parts"]} for d in c.get("dims") or [] if d["key"] != "opsiyon"},
                "fair": {k: (c.get("fair") or {}).get(k) for k in ("fair", "upside", "confidence")}}
    try:
        t = technicals.snapshot(sym)
    except Exception:  # noqa: BLE001
        log.exception("karar: technicals %s failed", sym)
        t = None
    tech = {k: t.get(k) for k in ("as_of", "price", "sma50", "sma200", "above50", "above200", "trend_score", "trend_label", "rs63_vs_spy",
                                  "from_hi52", "hi52", "chg_63d", "rsi14")} if t else None
    pos = None
    p = (ctx.get("positions") or {}).get(sym)
    rk = ctx.get("risk") or {}
    if p:
        h = next((x for x in rk.get("holdings") or [] if x["symbol"] == sym), {})
        cl = next((x for x in rk.get("clusters") or [] if sym in x["symbols"]), None)
        pos = {"weight": p["weight"], "unreal_pct": p["unreal_pct"], "value": p["value"], "avg": p["avg"],
               "risk_share": h.get("risk_share"), "beta": h.get("beta"), "cluster": cl}
    from .collectors import xbrl
    sec = [{k: r.get(k) for k in ("period_end", "form", "revenue", "rev_yoy", "net_income", "op_income", "shares_out")}
           for r in xbrl.quarter_table(sym, 6)]          # local SELECT on `fundamentals`, no network
    th = thesis.for_symbol(sym)
    thf = None
    if th and th["status"] != "idea":
        thf = {"id": th["id"], "status": th["status"], "next_review": th["next_review"], "review_due": th["review_due"],
               "rules": [{k: r.get(k) for k in ("id", "kind", "param", "label", "status", "last_value", "good")} for r in th["rules"]]}
    earn = [{"day": e["release_at"][:10], "name": e["name"]} for e in ctx.get("earnings") or [] if e["key"] == f"earnings:{sym}"]
    return {"symbol": sym, "today": today, "tech": tech, "card": card, "position": pos, "thesis": thf, "sec": sec,
            "news": {"events": _news_events(sym, today)}, "regime": _regime_brief(ctx.get("regime_row")), "earnings": earn,
            "position_as_of": ctx.get("position_as_of"), "risk_as_of": rk.get("as_of")}


def compute(symbol: str, ctx: dict | None = None) -> dict:
    return decide(gather(symbol, ctx))


def portfolio_facts(ctx: dict) -> dict:
    from . import regime
    reg = _regime_brief(ctx.get("regime_row"))
    prev = None
    if reg:
        hist = regime.history((date.fromisoformat(reg["day"]) - timedelta(days=14)).isoformat())
        hist = [h for h in hist if h["day"] <= reg["day"] and h["overall"] is not None]
        if len(hist) >= 6:
            prev = {"day": hist[-6]["day"], "score": hist[-6]["overall"]}
    rk = ctx.get("risk") or {}
    risk_f = {"as_of": rk.get("as_of"), "beta": rk.get("beta"), "stock_pct": rk.get("stock_pct"), "ceiling": rk.get("ceiling"),
              "warnings": [{"key": w["key"], "text": w["text"]} for w in rk.get("warnings") or []]} if rk and not rk.get("empty") else {}
    return {"as_of": ctx.get("position_as_of"), "regime": reg, "regime_prev": prev, "risk": risk_f, "recent_loss": _recent_loss(ctx)}


def _recent_loss(ctx: dict) -> dict | None:
    """Behavior pattern 'new trade within 3 days of a loss' — only when a losing trip closed in the last 3 days."""
    try:
        from . import behavior, portfolio
        today = date.fromisoformat(ctx["today"])
        trips = portfolio.closed_positions(ctx.get("led"))
        recent = [t for t in trips if t["realized"] < 0 and 0 <= (today - date.fromisoformat(t["close"][:10])).days <= behavior.POST_LOSS_DAYS]
        if not recent:
            return None
        pl = behavior.compute()["post_loss"]
        a, b = pl["after"], pl["base"]
        txt = f"Son {behavior.POST_LOSS_DAYS} günde zararla kapanan işlem var ({', '.join(sorted({t['symbol'] for t in recent}))})."
        if a["n"] and a.get("exp") is not None and b.get("exp") is not None:
            txt += (f" Geçmişte zarardan sonraki {behavior.POST_LOSS_DAYS} gün içinde açılan {a['n']} işlemin ortalama sonucu {tr_n(a['exp'], 0, True)}$, "
                    f"diğerlerinin {tr_n(b['exp'], 0, True)}$; yeni işlemden önce ön kontrolü kullan.")
        return {"text": txt, "symbols": sorted({t["symbol"] for t in recent})}
    except Exception:  # noqa: BLE001
        log.exception("karar: behavior check failed")
        return None


# ── model commentary ─────────────────────────────────────

def fingerprint(card: dict) -> str:
    keys = sorted(s["key"] for k in ("for", "against", "notes") for s in card.get(k) or [])
    return hashlib.sha1((card["stance"] + "|" + ",".join(keys)).encode()).hexdigest()[:16]


def card_facts(c: dict) -> dict:
    pos = c.get("position") or {}
    return {"sembol": c["symbol"], "tarih": c.get("as_of"), "durum": c["stance_tr"], "desen": c["pattern_tr"], "baslik": c["headline"],
            "lehine": [s["text"] for s in c["for"][:4]], "aleyhine": [s["text"] for s in c["against"][:4]], "notlar": [s["text"] for s in c["notes"][:3]],
            "degistirir": {"iyilesme": [x["text"] for x in c["changers"]["up"][:3]], "kotulesme": [x["text"] for x in c["changers"]["down"][:3]]},
            "siradaki": [f"{x['day']}: {x['text']}" for x in c["next"][:3]],
            "pozisyon": {"agirlik_yuzde": round(pos["weight"], 1) if pos.get("weight") is not None else None,
                         "kz_yuzde": round(pos["unreal_pct"], 1) if pos.get("unreal_pct") is not None else None} if pos else None}


def board_facts(b: dict) -> dict:
    return {"tarih": b.get("as_of"), "baslik": b["headline"], "piyasa": (b.get("market") or {}).get("text"),
            "dagilim": [{"durum": x["stance_tr"], "adet": x["n"], "agirlik_yuzde": x["weight_pct"], "hisseler": x["symbols"]} for x in b["stance_counts"]],
            "gundem": [a["text"] for a in b["agenda"]]}


def guard(text, facts: list[dict], max_words: int) -> str | None:
    """Plain text with only numbers present in the facts, no order verbs, within the word limit; else None."""
    from .scorecard import _numbers
    if not isinstance(text, str):
        return None
    text = " ".join(text.strip().strip('"').split())
    if not text or len(text.split()) > max_words or BANNED.search(text):
        return None
    allowed = set(ALLOW_NUMS)
    for f in facts:
        allowed |= _numbers(json.dumps(f, ensure_ascii=False, default=str))
    bad = _numbers(text) - allowed
    if bad:
        log.warning("karar comment rejected (%s): %s", bad, text[:160])
        return None
    return text


def prompt(cards: list[dict], b: dict) -> str:
    payload = json.dumps({"portfoy": board_facts(b), "hisseler": [card_facts(c) for c in cards]}, ensure_ascii=False, default=str)
    return ("Aşağıda bir yatırımcının portföyündeki her hisse için sunucunun kurallarla hesapladığı karar kartları ve portföy özeti var. "
            "Durum etiketlerini sunucu belirledi; sen yalnızca kısa yorum yazacaksın.\n"
            "Her hisse için en fazla 60 kelime: şirket ile fiyat arasındaki gerilimi sade sözlerle anlat, şu an en önemli tek şeyi söyle, "
            "sırada neyin izleneceğini belirt. Portföy için en fazla 90 kelime: piyasa nerede, bu portföy için ne anlama geliyor, gündemdeki ilk madde.\n"
            "Kurallar: Türkçe, düz metin (markdown yok); YALNIZCA verilen verilerdeki sayıları kullan, yeni sayı, tarih veya yüzde uydurma; "
            "al/sat/ekle/azalt gibi alım-satım talimatı ya da emir kipi kullanma; durum etiketini tekrar etme; verilerde olmayan hiçbir şeyden söz etme.\n"
            'Yanıt yalnızca şu JSON olsun: {"portfoy": "...", "hisseler": {"SEMBOL": "..."}}\n\n' + payload)


def write_comments(cards: list[dict], b: dict, prev: dict[str, dict], prev_board: dict | None, completer=None, now: datetime | None = None) -> bool:
    """Fill comment / comment_model on cards and board. Reuses the previous comments (no call) when every card's
    fingerprint is unchanged and the previous comments are younger than COMMENT_TTL days. Returns True if the model was called."""
    now = now or utcnow()
    for c in cards:
        c["fingerprint"] = fingerprint(c)

    def fresh(x):  # a non-empty comment younger than the TTL (a rejected / missing text is retried on the next run)
        ts = (x or {}).get("comment_at")
        return bool(ts) and bool((x or {}).get("comment")) and now - datetime.fromisoformat(ts) < timedelta(days=COMMENT_TTL)
    if cards and prev_board and fresh(prev_board) and all(c["symbol"] in prev and prev[c["symbol"]].get("fingerprint") == c["fingerprint"]
                                                          and fresh(prev[c["symbol"]]) for c in cards):
        for c in cards:
            p = prev[c["symbol"]]
            c["comment"], c["comment_model"], c["comment_at"] = p.get("comment"), p.get("comment_model"), p["comment_at"]
        b["comment"], b["comment_model"], b["comment_at"] = prev_board.get("comment"), prev_board.get("comment_model"), prev_board["comment_at"]
        return False
    from . import llm
    if completer is None:
        def completer(pr):
            routes = [(db.get_setting("karar_llm_provider"), db.get_setting("karar_llm_model"))] + llm.default_routes()
            return llm.complete(pr, routes=routes, priority=True, timeout=300)
    try:
        text, model = completer(prompt(cards, b))
        data = llm.parse_json(text)
    except llm.LLMError as exc:
        log.warning("karar comments skipped: %s", exc)
        return True
    stamp = now.isoformat(timespec="seconds")
    per = (data.get("hisseler") if isinstance(data, dict) else None) or {}
    bf = board_facts(b)
    for c in cards:
        txt = guard(per.get(c["symbol"]), [card_facts(c)], 75)
        c["comment"], c["comment_model"], c["comment_at"] = txt, model if txt else None, stamp
    txt = guard(data.get("portfoy") if isinstance(data, dict) else None, [bf] + [card_facts(c) for c in cards], 110)
    b["comment"], b["comment_model"], b["comment_at"] = txt, model if txt else None, stamp
    return True


# ── storage ──────────────────────────────────────────────

_COLS = ("id", "created_at", "comment", "comment_model")


def _load(r) -> dict:
    d = json.loads(r["body"])
    d.update({k: r[k] for k in _COLS})
    d.update(symbol=r["symbol"], as_of=r["as_of"], price=r["price"], stance=r["stance"], fingerprint=r["fingerprint"])
    d.update(_stance(r["stance"]) if r["stance"] in STANCES else {})
    return d


def latest(symbol: str) -> dict | None:
    """Newest stored card for the symbol, whatever run it came from (history)."""
    r = db.query_one("SELECT * FROM verdicts WHERE symbol = ? ORDER BY id DESC LIMIT 1", (symbol.upper(),))
    return _load(r) if r else None


def current(symbol: str) -> dict | None:
    """The symbol's card from the latest run only; None when that run has no card for it (e.g. position sold)."""
    r = db.query_one("SELECT v.* FROM verdicts v WHERE v.symbol = ? AND v.run_id = (SELECT MAX(id) FROM verdict_runs) ORDER BY v.id DESC LIMIT 1",
                     (symbol.upper(),))
    return _load(r) if r else None


def _changed(card: dict) -> dict | None:
    if not card.get("as_of"):
        return None
    r = db.query_one("SELECT stance, as_of FROM verdicts WHERE symbol = ? AND as_of < ? ORDER BY as_of DESC, id DESC LIMIT 1",
                     (card["symbol"], card["as_of"]))
    if r and r["stance"] != card["stance"] and r["stance"] in STANCES:
        return {"from": r["stance"], "from_tr": STANCES[r["stance"]][0], "day": card["as_of"]}
    return None


def save(b: dict) -> int:
    now = utcnow_iso()
    body = {k: v for k, v in b.items() if k not in ("cards", "comment", "comment_model", "run")}
    with db.tx() as c:
        rid = c.execute("INSERT INTO verdict_runs(created_at, as_of, body, comment, comment_model) VALUES (?,?,?,?,?)",
                        (now, b.get("as_of"), json.dumps(body, ensure_ascii=False, default=str), b.get("comment"), b.get("comment_model"))).lastrowid
        for k in b["cards"]:
            kb = {x: v for x, v in k.items() if x not in _COLS and x not in ("symbol", "as_of", "price", "stance", "fingerprint")}
            c.execute("INSERT INTO verdicts(run_id, symbol, created_at, as_of, price, stance, pattern, rules_ver, fingerprint, body, comment, comment_model) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, k["symbol"], now, k.get("as_of"), k.get("price"), k["stance"], k.get("pattern"), RULES_VER, k.get("fingerprint") or fingerprint(k),
                       json.dumps(kb, ensure_ascii=False, default=str), k.get("comment"), k.get("comment_model")))
    return rid


def board() -> dict | None:
    r = db.query_one("SELECT * FROM verdict_runs ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    b = json.loads(r["body"])
    cards = [_load(x) for x in db.query("SELECT * FROM verdicts WHERE run_id = ?", (r["id"],))]
    b["cards"] = sorted(cards, key=lambda c: (-c["rank"], -((c.get("position") or {}).get("weight") or 0)))
    b.update(comment=r["comment"], comment_model=r["comment_model"], run={"id": r["id"], "created_at": r["created_at"]})
    return b


def history(symbol: str, limit: int = 30) -> list[dict]:
    rows = db.query("SELECT id, created_at, as_of, price, stance FROM verdicts WHERE symbol = ? ORDER BY id DESC LIMIT ?", (symbol.upper(), limit))
    return [dict(r) | {"stance_tr": STANCES.get(r["stance"], (r["stance"], "neu", 0))[0], "tone": STANCES.get(r["stance"], ("", "neu", 0))[1]} for r in rows]


def run(symbols: list[str] | None = None, use_llm: bool = True, completer=None) -> dict | None:
    """Compute every held stock (or the given symbols), write comments (one model call at most), save; returns board()."""
    ctx = context()
    syms = [s.upper() for s in symbols] if symbols else sorted(ctx["positions"])
    cards = []
    for s in syms:
        try:
            c = compute(s, ctx)
        except Exception:  # noqa: BLE001 - one broken symbol must not stop the run
            log.exception("karar %s failed", s)
            continue
        c["changed"] = _changed(c)
        cards.append(c)
    b = build_board(cards, portfolio_facts(ctx))
    for c in cards:
        c["fingerprint"] = fingerprint(c)
    if use_llm and cards:
        prev = {c["symbol"]: p for c in cards if (p := latest(c["symbol"]))}
        pr = db.query_one("SELECT body, comment, comment_model FROM verdict_runs ORDER BY id DESC LIMIT 1")
        prev_board = (json.loads(pr["body"]) | {"comment": pr["comment"], "comment_model": pr["comment_model"]}) if pr else None
        try:
            write_comments(cards, b, prev, prev_board, completer)
        except Exception:  # noqa: BLE001 - the verdicts are saved without comments
            log.exception("karar comments failed")
    save(b)
    return board()


def brief(card: dict | None) -> dict | None:
    """Compact JSON for the Hermes context."""
    if not card:
        return None
    pos = card.get("position") or {}
    return {"symbol": card["symbol"], "as_of": card.get("as_of"), "stance": card["stance"], "stance_tr": card["stance_tr"],
            "pattern": card.get("pattern_tr"), "headline": card["headline"], "comment": card.get("comment"),
            "for": [s["text"] for s in card.get("for", [])[:3]], "against": [s["text"] for s in card.get("against", [])[:3]],
            "changers": {"up": [x["text"] for x in card["changers"]["up"][:3]], "down": [x["text"] for x in card["changers"]["down"][:3]]},
            "next": card.get("next", [])[:3], "todo": [x["text"] for x in card.get("todo", [])[:3]],
            "weight_pct": None if pos.get("weight") is None else round(pos["weight"], 1),
            "unreal_pct": None if pos.get("unreal_pct") is None else round(pos["unreal_pct"], 1),
            "note": "Kural tabanlı durum etiketi; alım/satım önerisi değildir."}


# ── track record ─────────────────────────────────────────

def track_record(horizon: int = 20) -> dict:
    """Forward `horizon`-session excess return vs SPY per stance, one card per (symbol, as_of)."""
    cards = db.query("""SELECT symbol, as_of, price, stance FROM verdicts WHERE as_of IS NOT NULL AND price IS NOT NULL
                        AND id IN (SELECT MAX(id) FROM verdicts GROUP BY symbol, as_of)""")
    spy = prices.series(config.CALENDAR_SYMBOL)
    idx = {d: i for i, d in enumerate(spy.days)} if spy else {}
    out = {k: {"stance": k, "label": v[0], "tone": v[1], "n": 0, "hits": 0, "sum": 0.0} for k, v in sorted(STANCES.items(), key=lambda kv: -kv[1][2])}
    cache: dict[str, prices.Series] = {}
    for c in cards:
        i = idx.get(c["as_of"])
        if i is None or i + horizon >= len(spy.days) or c["stance"] not in out:
            continue
        s = cache.setdefault(c["symbol"], prices.series(c["symbol"]))
        p1 = s.at(spy.days[i + horizon])
        if not p1:
            continue
        excess = (p1 / c["price"] - 1) * 100 - (spy.closes[i + horizon] / spy.closes[i] - 1) * 100
        b = out[c["stance"]]
        b["n"] += 1
        b["sum"] += excess
        b["hits"] += 1 if (c["stance"] == "saglam" and excess > 0) or (c["stance"] in ("sorgula", "alarm") and excess < 0) else 0
    rows = []
    for b in out.values():
        scored = b["stance"] in ("saglam", "sorgula", "alarm")
        rows.append({"stance": b["stance"], "label": b["label"], "tone": b["tone"], "n": b["n"], "hits": b["hits"] if scored else None,
                     "avg": b["sum"] / b["n"] if b["n"] else None, "hit": b["hits"] / b["n"] * 100 if b["n"] and scored else None})
    return {"horizon": horizon, "rows": rows, "n": sum(r["n"] for r in rows), "cards": len(cards)}


def daily_job() -> None:
    try:
        b = run(use_llm=True)
        log.info("karar: %d cards", len((b or {}).get("cards") or []))
    except Exception:  # noqa: BLE001
        log.exception("karar daily job failed")
