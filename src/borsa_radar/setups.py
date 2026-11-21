"""Daily setup scanner with a measured track record.

Rule-based detectors (pullback, breakout, bull flag, oversold bounce, range,
post-earnings drift) run on daily closes from the shared price store. Every
level (entry band, stop, two targets) and every sentence on a card is computed
here; the model writes at most the one "Yani" summary of the whole report.

Each setup is stored and followed bar by bar on closing prices:
  waiting → triggered (close inside/above the entry band) → target 1 / target 2 /
  stop / time exit; or expired (no trigger within its validity window).
Hit rate = reached target 1 / (target 1 + stop); untriggered expiries are excluded.
History is backfilled by running the same detectors on past days ("geriye dönük
test"), kept apart from live calls.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from . import config, db, prices
from .util import utcnow_iso

log = logging.getLogger(__name__)

RULES_VER = "s1"
MIN_RR = 1.5
MAX_CARDS = 5
HOLD_MAX = 20            # sessions after the trigger before a time exit
MIN_DOLLAR_VOL = 5e6     # 20-day average close × volume
MIN_QUALITY = 50         # below this a setup is neither shown nor counted in the record
DISCOVERY_CARDS = 5      # discovery tier (index members outside the own list): picks shown per day
DISCOVERY_MIN_QUALITY = 70   # ... and only "Güçlü" ones, since these are names the user does not follow
DISCOVERY_PER_SECTOR = 2
DISCOVERY_MIN_STOP_ATR = 1.0  # a stop inside one day's normal range inflates R:R; such setups are not shown from the wide tier
_OWN = "COALESCE(tier, 'liste') != 'kesif'"            # SQL: own-list rows
_VISIBLE = f"({_OWN} OR picked = 1)"                    # SQL: rows the pages, digests and alerts deal with
KINDS = {
    "pullback": ("Geri Çekilme", "Pullback"),
    "breakout": ("Kırılım", "Breakout"),
    "flag": ("Boğa Bayrağı", "Bull Flag"),
    "bounce": ("Aşırı Satış Sıçraması", "Bounce"),
    "range": ("Bant Alımı", "Range"),
    "earnings": ("Bilanço Sonrası", "Post-earnings"),
}
VALID_DAYS = {"pullback": 4, "breakout": 5, "flag": 5, "bounce": 3, "range": 4, "earnings": 5}
OPTION_IDEA = {
    "pullback": "Long call · ~0,40–0,50 delta · 4–6 hafta vade (alternatif: ATM dikey borç spread'i)",
    "breakout": "Long call · ~0,45–0,55 delta · 2–4 hafta vade (alternatif: dikey borç spread'i)",
    "flag": "Long call · ~0,45–0,55 delta · 2–4 hafta vade (alternatif: dikey borç spread'i)",
    "bounce": "Kısa vadeli: ATM dikey borç spread'i · 2–3 hafta (çıplak call yerine; oynaklık yüksek)",
    "range": "Bant tabanından: dikey borç spread'i, üst bacak bant tavanında · 3–4 hafta",
    "earnings": "Long call · ~0,50 delta · 3–5 hafta vade (bilanço sonrası IV düştüğü için prim ucuzlar)",
}
STATUS_TR = {"waiting": "Bekliyor", "triggered": "TETİKLENDİ", "t1": "Hedef 1 ✓", "t2": "Hedef 2 ✓", "stop": "Stop",
             "time": "Süre doldu (pozisyonda)", "expired": "Tetiklenmedi", "invalid": "İptal (tetik öncesi stop altı)"}


# ── indicator arrays (computed once per symbol, reused for every day) ──

class Arr:
    def __init__(self, bars: list[dict]):
        self.days = [b["day"] for b in bars]
        self.c = [b["close"] for b in bars]
        self.h = [b["high"] if b["high"] is not None else b["close"] for b in bars]
        self.l = [b["low"] if b["low"] is not None else b["close"] for b in bars]
        self.o = [b["open"] if b["open"] is not None else b["close"] for b in bars]
        self.v = [b["volume"] or 0 for b in bars]
        n = len(bars)
        self.ema20, self.ema50 = _ema(self.c, 20), _ema(self.c, 50)
        self.sma50, self.sma200 = _sma(self.c, 50), _sma(self.c, 200)
        self.rsi = _rsi(self.c, 14)
        self.atr = _atr(self.h, self.l, self.c, 14)
        self.vol20 = _sma([float(x) for x in self.v], 20)
        self.n = n


def _sma(xs, n):
    out, acc = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= n:
            acc -= xs[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out


def _ema(xs, n):
    out, k = [], 2 / (n + 1)
    for i, x in enumerate(xs):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return [v if i >= n - 1 else None for i, v in enumerate(out)]


def _rsi(xs, n):
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    g = sum(max(xs[i] - xs[i - 1], 0) for i in range(1, n + 1)) / n
    lo = sum(max(xs[i - 1] - xs[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100 - 100 / (1 + g / lo) if lo else 100.0
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        lo = (lo * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + g / lo) if lo else 100.0
    return out


def _atr(h, l, c, n):
    out = [None] * len(c)
    trs = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    if len(c) <= n:
        return out
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ── setup model ───────────────────────────────────────────

@dataclass
class Setup:
    symbol: str
    kind: str
    day: str                 # base day (signal close)
    entry_lo: float
    entry_hi: float
    stop: float
    t1: float
    t2: float
    entry_why: str
    stop_why: str
    t1_why: str
    t2_why: str
    status_line: str         # one-line "what is going on"
    trigger: str             # Tetik paragraph
    playbook: str
    yani: str
    band: tuple | None = None
    band_days: int | None = None
    quality: float = 0.0
    reasons: list = field(default_factory=list)
    ind: dict = field(default_factory=dict)

    @property
    def entry(self) -> float:
        return (self.entry_lo + self.entry_hi) / 2

    @property
    def risk(self) -> float:
        return self.entry - self.stop

    @property
    def rr(self) -> float:
        """Weighted R:R: half off at target 1, half at target 2."""
        if self.risk <= 0:
            return 0.0
        return (0.5 * (self.t1 - self.entry) + 0.5 * (self.t2 - self.entry)) / self.risk


def tr_money(v: float | None, d: int = 2) -> str:
    if v is None:
        return "—"
    return (f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")) + " $"


def tr_num(v: float | None, d: int = 1) -> str:
    return "—" if v is None else f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def tr_day(day: str) -> str:
    m = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
    return f"{int(day[8:10])} {m[int(day[5:7]) - 1]}"


_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
_LOC = ["'ta", "'ta", "'ta", "'da", "'ta", "'da", "'da", "'ta", "'de", "'de", "'da", "'ta"]   # Ocak'ta, Nisan'da, Eylül'de …
_ABL = ["'tan", "'tan", "'tan", "'dan", "'tan", "'dan", "'dan", "'tan", "'den", "'den", "'dan", "'tan"]


def tr_de(day: str) -> str:
    """'4 Kasım'da'"""
    m = int(day[5:7]) - 1
    return f"{int(day[8:10])} {_MONTHS[m]}{_LOC[m]}"


def tr_den(day: str) -> str:
    m = int(day[5:7]) - 1
    return f"{int(day[8:10])} {_MONTHS[m]}{_ABL[m]}"


def _argmax(xs, lo, hi):
    j = max(range(lo, hi), key=lambda k: xs[k])
    return j, xs[j]


def _argmin(xs, lo, hi):
    j = min(range(lo, hi), key=lambda k: xs[k])
    return j, xs[j]


def _targets_above(a: Arr, i: int, ref: float, atr: float) -> list[tuple[float, str]]:
    """Resistance candidates above `ref`: recent swing highs and the 52-week high."""
    cands = []
    for look, name in ((20, "20 günlük zirve"), (60, "3 aylık zirve"), (252, "52 hafta zirvesi")):
        lo = max(0, i - look + 1)
        j, hv = _argmax(a.h, lo, i + 1)
        if hv > ref + 0.3 * atr:
            cands.append((hv, f"{name} ({tr_day(a.days[j])})"))
    cands.sort()
    out = []
    for v, why in cands:
        if not out or v > out[-1][0] + 0.25 * atr:
            out.append((v, why))
    return out


def _ensure_two(entry: float, atr: float, ts: list[tuple[float, str]], risk: float) -> tuple[tuple, tuple]:
    """Pick T1/T2; fill missing ones with ATR / R multiples (labelled)."""
    ts = [t for t in ts if t[0] > entry + 0.5 * atr]
    if not ts:
        ts = [(entry + max(2 * risk, 2 * atr), "2R / 2 ATR projeksiyonu")]
    if len(ts) == 1:
        ts.append((max(ts[0][0] + atr, entry + 3 * risk), "3R projeksiyonu"))
    return ts[0], ts[1]


def _base(a: Arr, i: int) -> dict | None:
    if i < 200 or a.atr[i] is None or a.sma200[i] is None or a.vol20[i] is None:
        return None
    return {"px": a.c[i], "ema20": a.ema20[i], "ema50": a.ema50[i], "sma50": a.sma50[i], "sma200": a.sma200[i],
            "rsi": a.rsi[i], "atr": a.atr[i], "vol": a.v[i], "vol20": a.vol20[i]}


# ── detectors: each returns (Setup | None, rejection reason | None) ──

def det_pullback(sym: str, a: Arr, i: int):
    b = _base(a, i)
    if not b:
        return None, None
    px, e20, e50, s200, atr, r = b["px"], b["ema20"], b["ema50"], b["sma200"], b["atr"], b["rsi"]
    if not (e20 > e50 and px > s200 and e50 > s200):
        return None, "geri çekilme alımı için gereken yukarı trend yapısı yok (EMA20 > EMA50 > SMA200)"
    hi10_j, hi10 = _argmax(a.c, i - 10, i + 1)
    drop = (px / hi10 - 1) * 100
    if not (e50 - 0.25 * atr <= px <= e20 + 0.6 * atr):
        if px > e20 + 0.6 * atr:
            return None, f"fiyat EMA20'nin ({tr_money(e20)}) %{tr_num((px / e20 - 1) * 100)} üstünde, geri çekilme yok"
        return None, f"fiyat EMA50'nin ({tr_money(e50)}) altına sarktı, geri çekilme derinleşti"
    if drop > -2.5 or not (35 <= (r or 0) <= 58):
        return None, "anlamlı bir geri çekilme yok (son 10 günün zirvesinden %2,5'ten az düşüş veya RSI 35–58 dışında)"
    vol5 = sum(a.v[i - 4:i + 1]) / 5
    trig = max(e20, a.h[i])
    entry_lo, entry_hi = trig + 0.02 * atr, trig + 0.12 * atr
    lj, low5 = _argmin(a.l, i - 4, i + 1)
    stop = low5 - 0.1 * atr
    if entry_lo - stop > 2.6 * atr:
        stop = entry_lo - 2.6 * atr
        stop_why = "2,6 ATR (dip çok uzakta)"
    else:
        stop_why = f"{tr_day(a.days[lj])} dibi altı"
    t1, t2 = _ensure_two((entry_lo + entry_hi) / 2, atr, _targets_above(a, i, entry_hi, atr), (entry_lo + entry_hi) / 2 - stop)
    s = Setup(sym, "pullback", a.days[i], entry_lo, entry_hi, stop, t1[0], t2[0],
              f"EMA20 + {tr_day(a.days[i])} yükseği üstü", stop_why, t1[1], t2[1],
              status_line="Trend yukarı, fiyat 20 günlük ortalamaya geri çekilip dinleniyor; tetik henüz gelmedi.",
              trigger=(f"{sym} {tr_de(a.days[hi10_j])} {tr_money(hi10)} kapanıştan {tr_money(px)}'a geri çekildi (%{tr_num(drop)}). "
                       f"RSI {tr_num(r)}. EMA20 ({tr_money(e20)}) hâlâ EMA50'nin ({tr_money(e50)}) üstünde, trend yapısı sağlam. "
                       f"Son 5 günün hacmi 20 günlük ortalamaya göre %{tr_num(vol5 / b['vol20'] * 100, 0)}" + (" (satış baskısı zayıf). " if vol5 < 0.8 * b['vol20'] else ". ") + 
                       f"Tetik, {tr_money(trig)} üstünde günlük kapanıştır."),
              playbook=_playbook(b, stop_why),
              yani="Güçlü bir hisse kısa bir mola veriyor; 20 günlük ortalamanın üstüne dönüş, yükselişin sürdüğünü teyit eder.")
    s.reasons = [("trend", 1.0), ("low_vol_pullback", 1.0 if vol5 < b["vol20"] else 0.4)]
    return s, None


def det_breakout(sym: str, a: Arr, i: int):
    b = _base(a, i)
    if not b:
        return None, None
    px, atr = b["px"], b["atr"]
    if px < b["sma50"]:
        return None, None
    rj, res = _argmax(a.h, i - 60, i - 2)
    if not (0.97 * res <= px <= res):
        return None, None
    lo20 = min(a.l[i - 19:i + 1])
    hi20 = max(a.h[i - 19:i + 1])
    width = (hi20 - lo20) / px * 100
    atr_prev = a.atr[i - 20]
    if width > 14 or (atr_prev and atr > atr_prev * 1.05):
        return None, f"kırılım için gereken sıkışma yok (20 günlük bant %{tr_num(width)} geniş veya ATR genişliyor)"
    entry_lo, entry_hi = res + 0.05 * atr, res + 0.25 * atr
    mid = (lo20 + hi20) / 2
    stop = max(mid, entry_lo - 2.5 * atr)
    height = hi20 - lo20
    ts = [t for t in _targets_above(a, i, entry_hi, atr) if t[0] > res + 0.5 * atr]
    ts.append((res + height, "bant yüksekliği projeksiyonu"))
    ts.sort()
    t1, t2 = _ensure_two((entry_lo + entry_hi) / 2, atr, ts, (entry_lo + entry_hi) / 2 - stop)
    s = Setup(sym, "breakout", a.days[i], entry_lo, entry_hi, stop, t1[0], t2[0],
              f"{tr_money(res)} direnci üstü ({tr_day(a.days[rj])} yükseği)", "sıkışma bandının ortası altı" if stop == mid else "2,5 ATR",
              t1[1], t2[1], band=(lo20, hi20), band_days=20,
              status_line="Fiyat direncin hemen altında daralan bir bantta; tetik direnç üstü kapanıştır.",
              trigger=(f"{sym} son 20 seansta {tr_money(lo20)}–{tr_money(hi20)} arasında, %{tr_num(width)} genişliğinde bir bantta sıkıştı. "
                       f"Fiyat ({tr_money(px)}), {tr_de(a.days[rj])} oluşan {tr_money(res)} direncinin %{tr_num((1 - px / res) * 100)} altında. "
                       f"Tetik, {tr_money(res)} üstünde günlük kapanıştır."),
              playbook=(f"Kırılım günü hacim 20 günlük ortalamanın ({tr_num(b['vol20'] / 1e6)} M) üstünde olmalı; değilse yarım pozisyon. "
                        f"Kırılımdan sonra kapanış yeniden {tr_money(res)} altına dönerse çıkılır. Hedef 1'de yarısı satılır, stop başa başa çekilir."),
              yani="Hisse bir süredir aynı tavana çarpıyor; tavanın üstünde kapanış yeni bir yükseliş ayağını başlatabilir.")
    s.reasons = [("tight", 1.0 if width < 8 else 0.6), ("trend", 1.0 if px > b["sma200"] else 0.3)]
    return s, None


def det_flag(sym: str, a: Arr, i: int):
    b = _base(a, i)
    if not b:
        return None, None
    atr, px = b["atr"], b["px"]
    best = None
    for flag_len in range(3, 11):
        top_i = i - flag_len
        if top_i - 8 < 0:
            break
        top = a.c[top_i]
        if top < max(a.c[top_i - 2:i + 1]) - 1e-9 and max(a.c[top_i + 1:i + 1]) > top:
            continue  # pole top must be the highest close since
        start_j, start = _argmin(a.c, top_i - 8, top_i)
        pole = top - start
        if pole / start < 0.10 or pole < 3 * atr:
            continue
        flag_hi = max(a.h[top_i:i + 1])
        flag_lo = min(a.l[top_i + 1:i + 1])
        if (top - flag_lo) > 0.5 * pole or px < b["ema20"]:
            continue
        best = (flag_len, top_i, top, start_j, start, pole, flag_hi, flag_lo)
        break
    if not best:
        return None, None
    flag_len, top_i, top, start_j, start, pole, flag_hi, flag_lo = best
    vol_pole = max(a.v[start_j:top_i + 1])
    entry_lo, entry_hi = flag_hi + 0.03 * atr, flag_hi + 0.18 * atr
    stop = max(flag_lo - 0.1 * atr, entry_lo - 2.6 * atr)
    ts = _targets_above(a, i, entry_hi, atr) + [(flag_hi + pole, "bayrak direği projeksiyonu")]
    ts.sort()
    t1, t2 = _ensure_two((entry_lo + entry_hi) / 2, atr, ts, (entry_lo + entry_hi) / 2 - stop)
    s = Setup(sym, "flag", a.days[i], entry_lo, entry_hi, stop, t1[0], t2[0],
              f"Bayrak üstü {tr_money(flag_hi)} kırılımı", "bayrak dibi altı", t1[1], t2[1], band=(flag_lo, flag_hi), band_days=flag_len,
              status_line="Sert yükselişin ardından fiyat dar bir bantta soluklanıyor; tetik bayrak üst sınırının kırılmasıdır.",
              trigger=(f"{sym} {tr_de(a.days[start_j])} {tr_money(start)} iken {tr_de(a.days[top_i])} {tr_money(top)} seviyesine yükseldi "
                       f"(%{tr_num(pole / start * 100)}); bu bayrak direğidir. O günden beri fiyat {tr_money(flag_lo)} ile {tr_money(flag_hi)} arasında. "
                       f"Günlük hacim direkteki {tr_num(vol_pole / 1e6)} M zirveden son seansta {tr_num(a.v[i] / 1e6)} M seviyesine geriledi. "
                       f"Tetik, {tr_money(flag_hi)} üstünde günlük kapanıştır."),
              playbook=(f"Kırılım günü kapanış {tr_money(flag_hi)} üstünde olmalı; gün içi aşıp altında kapanırsa giriş yok. "
                        f"Kırılım sonrası kapanış yeniden bayrağın içine dönerse pozisyon kapatılır. Hedef 1'de yarısı satılır, kalan Hedef 2'ye taşınır."),
              yani="Hisse hızlı yükselişin ardından nefesleniyor; bandın üstüne çıkarsa yükseliş devam edebilir.")
    s.reasons = [("vol_dry", 1.0 if a.v[i] < 0.7 * vol_pole else 0.5), ("trend", 1.0 if px > b["sma200"] else 0.4)]
    return s, None


def det_bounce(sym: str, a: Arr, i: int):
    b = _base(a, i)
    if not b:
        return None, None
    px, atr, r = b["px"], b["atr"], b["rsi"]
    if px < b["sma200"] * 0.97:
        return None, None
    downs = sum(1 for k in range(i - 4, i + 1) if a.c[k] < a.c[k - 1])
    if r is None or r > 32 or downs < 3:
        return None, None
    trig = a.h[i]
    entry_lo, entry_hi = trig + 0.02 * atr, trig + 0.15 * atr
    lj, low = _argmin(a.l, i - 4, i + 1)
    stop = max(low - 0.25 * atr, entry_lo - 2.5 * atr)
    ts = [(b["ema20"], "EMA20 (ortalamaya dönüş)")] if b["ema20"] > entry_hi + 0.5 * atr else []
    ts += [(b["sma50"], "50 günlük ortalama")] if b["sma50"] > entry_hi + 0.5 * atr else []
    ts += _targets_above(a, i, entry_hi, atr)[:1]
    ts.sort()
    t1, t2 = _ensure_two((entry_lo + entry_hi) / 2, atr, ts, (entry_lo + entry_hi) / 2 - stop)
    s = Setup(sym, "bounce", a.days[i], entry_lo, entry_hi, stop, t1[0], t2[0],
              f"{tr_day(a.days[i])} yükseği üstü", f"{tr_day(a.days[lj])} dibi altı", t1[1], t2[1],
              status_line="Uzun vadeli trend yukarıyken fiyat kısa vadede aşırı satıldı; tetik bir önceki günün yükseği üstü kapanıştır.",
              trigger=(f"{sym} son 5 seansın {downs}'ünde düştü, RSI {tr_num(r)} ile aşırı satış bölgesinde. Fiyat ({tr_money(px)}) 200 günlük "
                       f"ortalamanın ({tr_money(b['sma200'])}) %{tr_num((px / b['sma200'] - 1) * 100)} {'üstünde' if px >= b['sma200'] else 'altında'}. "
                       f"Tetik, {tr_money(trig)} üstünde günlük kapanıştır."),
              playbook="Düşen bıçağı tutmamak için tetik beklenir. Sıçrama kısa vadelidir: Hedef 1'de en az yarısı satılır, kalan için stop başa başa çekilir.",
              yani="Sağlam bir hisse kısa sürede fazla satıldı; dönüş teyidi gelirse ortalamaya doğru bir tepki yükselişi beklenebilir.")
    s.reasons = [("oversold", 1.0 if r < 28 else 0.7)]
    return s, None


def det_range(sym: str, a: Arr, i: int):
    b = _base(a, i)
    if not b:
        return None, None
    atr, px = b["atr"], b["px"]
    look = 30
    hi = max(a.h[i - look + 1:i + 1])
    lo = min(a.l[i - look + 1:i + 1])
    width = (hi - lo) / lo * 100
    s50_then = a.sma50[i - 20]
    if not (5 <= width <= 16) or not s50_then or abs(b["sma50"] / s50_then - 1) > 0.03:
        return None, None
    tol = 0.2 * (hi - lo)
    touch_lo = sum(1 for k in range(i - look + 1, i + 1) if a.l[k] <= lo + tol and (k == i - look + 1 or a.l[k - 1] > lo + tol))
    touch_hi = sum(1 for k in range(i - look + 1, i + 1) if a.h[k] >= hi - tol and (k == i - look + 1 or a.h[k - 1] < hi - tol))
    if touch_lo < 2 or touch_hi < 2 or px > lo + 0.3 * (hi - lo):
        return None, None
    trig = a.h[i]
    entry_lo, entry_hi = trig + 0.02 * atr, trig + 0.12 * atr
    stop = lo - 0.25 * atr
    mid = (hi + lo) / 2
    s = Setup(sym, "range", a.days[i], entry_lo, entry_hi, stop, mid, hi - 0.1 * atr,
              f"{tr_day(a.days[i])} yükseği üstü (bant tabanında)", "bant tabanı altı", "bant ortası", "bant tavanı",
              band=(lo, hi), band_days=look,
              status_line="Fiyat yatay bir bandın tabanında; tetik tabandan dönüş kapanışıdır.",
              trigger=(f"{sym} son {look} seansta {tr_money(lo)}–{tr_money(hi)} bandında (genişlik %{tr_num(width)}); tabana {touch_lo}, tavana {touch_hi} kez değdi. "
                       f"Fiyat ({tr_money(px)}) bandın alt çeyreğinde. Tetik, {tr_money(trig)} üstünde günlük kapanıştır."),
              playbook="Bant işlemi: tavana yaklaşınca çıkılır, tavan kırılırsa kalan pozisyon Kırılım kurallarıyla izlenir. Taban altında kapanış = çıkış.",
              yani="Hisse belli bir aralıkta gidip geliyor ve şu an aralığın dibinde; tepki gelirse bandın ortasına/tavanına dönüş hedeflenir.")
    s.reasons = [("touches", min(1.0, (touch_lo + touch_hi) / 6))]
    return s, None


def det_earnings(sym: str, a: Arr, i: int, earn_days):
    """`earn_days` is a set or a zero-arg callable returning one (fetched only when a big gap exists)."""
    b = _base(a, i)
    if not b or not earn_days:
        return None, None
    atr = b["atr"]
    for k in range(i - 8, i - 1):
        gap = (a.o[k] / a.c[k - 1] - 1) * 100
        move = (a.c[k] / a.c[k - 1] - 1) * 100
        vol20 = a.vol20[k - 1]
        if gap < 3 or move < 4 or not vol20 or a.v[k] < 2 * vol20:
            continue
        if callable(earn_days):
            earn_days = earn_days()
        if a.days[k] not in earn_days and a.days[k - 1] not in earn_days:
            continue
        gap_lo = a.l[k]
        if min(a.c[k + 1:i + 1]) < gap_lo:
            return None, "bilanço sonrası boşluk korunamadı (fiyat boşluk gününün dibinin altına indi)"
        hi_post = max(a.h[k:i + 1])
        entry_lo, entry_hi = hi_post + 0.03 * atr, hi_post + 0.18 * atr
        stop = max(gap_lo - 0.1 * atr, entry_lo - 2.8 * atr)
        t1, t2 = _ensure_two((entry_lo + entry_hi) / 2, atr, _targets_above(a, i, entry_hi, atr), (entry_lo + entry_hi) / 2 - stop)
        s = Setup(sym, "earnings", a.days[i], entry_lo, entry_hi, stop, t1[0], t2[0],
                  f"bilanço sonrası zirve {tr_money(hi_post)} üstü", f"boşluk günü ({tr_day(a.days[k])}) dibi altı", t1[1], t2[1],
                  band=(min(a.l[k + 1:i + 1]), hi_post), band_days=i - k,
                  status_line="Bilanço sonrası güçlü hacimli boşluk korunuyor; tetik bilanço sonrası zirvenin aşılmasıdır.",
                  trigger=(f"{sym} {tr_de(a.days[k])} bilanço sonrası %{tr_num(gap)} boşlukla açıldı ve günü %{tr_num(move)} yükselişle kapattı; "
                           f"hacim 20 günlük ortalamanın {tr_num(a.v[k] / vol20)} katıydı. O günden beri fiyat boşluk gününün dibinin "
                           f"({tr_money(gap_lo)}) üstünde. Tetik, {tr_money(hi_post)} üstünde günlük kapanıştır."),
                  playbook="Bilanço sonrası kayma (PEAD) eğilimi: güçlü sürprizler haftalarca fiyatlanmaya devam edebilir. Boşluk günü dibinin altında kapanış tezi bozar.",
                  yani="Şirket beklentiyi aştı ve piyasa bunu hacimle onayladı; yükseliş genelde birkaç hafta devam edebilir.")
        s.reasons = [("gap", min(1.0, gap / 8))]
        return s, None
    return None, None


DETECTORS = [det_earnings, det_flag, det_breakout, det_pullback, det_bounce, det_range]


def _playbook(b: dict, stop_why: str) -> str:
    return (f"Tetik günü hacim 20 günlük ortalamanın ({tr_num(b['vol20'] / 1e6)} M) altındaysa yarım pozisyonla başlanır. "
            f"Stop ({stop_why}) kapanış bazlı izlenir; ATR {tr_money(b['atr'])} olduğundan gün içi iğneler stop'u yoklayabilir. "
            "Hedef 1'de yarısı satılır, stop başa baş noktasına çekilir, kalan Hedef 2'ye taşınır.")


def quality(s: Setup, a: Arr, i: int, spy: prices.Series | None, regime_score: float | None) -> float:
    """0-100: R:R, stop distance in ATR, trend stack, relative strength, volume, market regime, detector-specific bits."""
    atr = a.atr[i]
    pts = 0.0
    pts += min(25, max(0, (s.rr - 1) * 15))                    # R:R 1.5 → 7.5, 2.5 → 22.5
    stop_atr = s.risk / atr if atr else 9
    pts += 15 if 1.0 <= stop_atr <= 2.2 else 8 if stop_atr <= 2.8 else 0
    trend = sum((a.c[i] > (a.sma200[i] or 1e18), (a.ema20[i] or 0) > (a.ema50[i] or 1e18), (a.sma50[i] or 0) > (a.sma200[i] or 1e18)))
    pts += trend * 5
    if spy and i >= 63:
        sp0, sp1 = spy.at(a.days[i - 63]), spy.at(a.days[i])
        if sp0 and sp1:
            rs = (a.c[i] / a.c[i - 63] - 1) - (sp1 / sp0 - 1)
            pts += max(0, min(15, 7.5 + rs * 50))
    pts += sum(w for _, w in s.reasons) / max(1, len(s.reasons)) * 15
    if regime_score is not None:
        pts += max(0, min(15, (regime_score - 30) / 3))
    return round(min(100, pts), 1)


def strength(q: float) -> tuple[str, str]:
    return ("Güçlü", "pos") if q >= 70 else ("Orta", "warn") if q >= 50 else ("Zayıf", "neg")


# ── scanning ──────────────────────────────────────────────

def wide_lists() -> list[str]:
    """Index lists of the discovery tier (setting `setup_wide`, e.g. "sp500 sp400"); unknown codes are ignored."""
    from . import breadth
    return [c for c in (db.get_setting("setup_wide") or "").lower().replace(",", " ").split() if c in breadth.INDEX_LISTS]


def universe(wide: bool = False) -> list[dict]:
    """Own list (tier "liste"): portfolio stock holdings + watchlist + extra pool (setting `setup_extra`).
    wide=True adds the discovery tier ("kesif"): members of the index lists in `setup_wide` that are not on the own list,
    each with its company name and sector, since these are names the user may not know."""
    from . import portfolio
    out: dict[str, dict] = {}
    try:
        for p in portfolio.positions():
            if p["asset"] == "stock":
                out.setdefault(p["symbol"], {"symbol": p["symbol"], "src": f"{p['sub']}", "held": True, "tier": "liste"})
    except Exception:  # noqa: BLE001
        log.exception("portfolio positions failed")
    for t in db.active_tickers():
        out.setdefault(t["symbol"], {"symbol": t["symbol"], "src": "izleme", "held": False, "tier": "liste"})
    for s in (db.get_setting("setup_extra") or "").replace(",", " ").split():
        out.setdefault(s.upper(), {"symbol": s.upper(), "src": "havuz", "held": False, "tier": "liste"})
    if wide:
        from . import breadth
        for code in wide_lists():
            try:
                members = breadth.index_members(code).data or []
            except Exception:  # noqa: BLE001 - a list that cannot be read must not stop the own-list scan
                log.exception("index list %s failed", code)
                members = []
            label = breadth.INDEX_LISTS[code][1]
            for m in members:
                out.setdefault(m["symbol"], {"symbol": m["symbol"], "src": label, "held": False, "tier": "kesif",
                                             "company": m.get("name"), "sector": m.get("sector")})
    return list(out.values())


def base_day() -> str | None:
    """Last complete session: skip today's partial bar while the US session is still running."""
    lu = prices.last_update()
    if not lu:
        return None
    now = datetime.now(config.NY_TZ)
    day = lu["day"]
    if day == now.date().isoformat() and (now.hour, now.minute) < (16, 15):
        prev = db.query_one("SELECT day FROM prices_daily WHERE symbol = ? AND day < ? ORDER BY day DESC LIMIT 1",
                            (config.CALENDAR_SYMBOL, day))
        day = prev["day"] if prev else day
    return day


def _earn_days(sym: str, fmp: bool = True) -> set[str]:
    """fmp=False (discovery tier): calendar rows only, so a wide scan never spends the small FMP quota."""
    rows = db.query("SELECT release_at FROM macro_calendar WHERE key = ?", (f"earnings:{sym}",))
    out = set()
    for r in rows:
        d = datetime.fromisoformat(r["release_at"]).astimezone(config.NY_TZ)
        # after-close reports gap on the next session
        out.add(d.date().isoformat())
        out.add((d.date() + timedelta(days=1 if d.hour >= 16 else 0)).isoformat())
    if not fmp:
        return out
    try:
        from . import stockdata
        for e in stockdata.fmp("earnings", 24, symbol=sym, limit=5).data or []:
            if e.get("date"):
                dd = date.fromisoformat(e["date"][:10])
                out.update({dd.isoformat(), (dd + timedelta(days=1)).isoformat()})
    except Exception:  # noqa: BLE001
        pass
    return out


def scan_symbol(sym: str, a: Arr, i: int, spy, regime_score, earn: set[str] | None) -> tuple[list[Setup], str | None]:
    found, why, rr_why = [], None, None
    if i < 200 or a.vol20[i] is None:
        return [], "yeterli fiyat geçmişi yok (200 seans)"
    if a.vol20[i] * a.c[i] < MIN_DOLLAR_VOL:
        return [], f"günlük işlem hacmi ince (20g ort. {tr_num(a.vol20[i] * a.c[i] / 1e6)} M$)"
    for det in DETECTORS:
        s, r = det(sym, a, i, earn) if det is det_earnings else det(sym, a, i)
        if s:
            if s.risk <= 0 or s.t1 <= s.entry:
                continue
            if s.rr < MIN_RR:
                rr_why = rr_why or (f"{KINDS[s.kind][0].lower()} var ama ağırlıklı R:R 1:{tr_num(s.rr)} (eşik 1:{tr_num(MIN_RR)}); "
                              f"mantıklı stop {tr_money(s.stop)}, hedefler {tr_money(s.t1)} ve {tr_money(s.t2)}")
                continue
            s.quality = quality(s, a, i, spy, regime_score)
            s.ind = {"rsi": a.rsi[i], "ema20": a.ema20[i], "ema50": a.ema50[i], "sma200": a.sma200[i], "atr": a.atr[i],
                     "vol": a.v[i], "vol20": a.vol20[i], "px": a.c[i], "chg1": (a.c[i] / a.c[i - 1] - 1) * 100}
            found.append(s)
        elif r and not why:
            why = r
    if not found:
        px, s50, s200 = a.c[i], a.sma50[i], a.sma200[i]
        if px < s50 and px < s200:
            why = f"düşüş trendi: {tr_money(px)} (50g {tr_money(s50)}, 200g {tr_money(s200)})"
        elif rr_why:
            why = rr_why
        elif a.ema20[i] and px > a.ema20[i] + 2 * a.atr[i]:
            why = f"fiyat uzamış: EMA20'nin ({tr_money(a.ema20[i])}) %{tr_num((px / a.ema20[i] - 1) * 100)} üstünde, kovalama riski"
        elif px < s50 and px >= s200:
            why = f"200 günlüğün üstünde ama 50 günlüğün ({tr_money(s50)}) altında; trend yapısı bozuk, net kurulum yok"
        elif not why:
            why = "tanımlı kurulumların hiçbiri oluşmadı"
    found.sort(key=lambda s: -s.quality)
    return found[:1], None if found else why   # one setup per symbol per day: the best one


def _regime_score(day: str) -> tuple[float | None, str | None]:
    r = db.query_one("SELECT overall, label FROM regime_history WHERE day <= ? ORDER BY day DESC LIMIT 1", (day,))
    return (r["overall"], r["label"]) if r else (None, None)


def _why_group(why: str | None) -> str:
    """Short bucket for the discovery tier, where 'why not' is reported as counts instead of one line per name."""
    w = why or ""
    for key, label in (("düşüş trendi", "düşüş trendinde"), ("işlem hacmi ince", "işlem hacmi ince"), ("yeterli fiyat geçmişi yok", "fiyat geçmişi kısa"),
                       ("R:R", "risk-ödül oranı yetersiz"), ("fiyat uzamış", "fiyat uzamış"), ("50 günlüğün", "trend yapısı bozuk")):
        if key in w:
            return label
    return "tanımlı kurulum yok"


def scan(day: str | None = None, *, source: str = "live", refresh: bool = True, wide: bool = True) -> dict:
    day = day or base_day()
    if not day:
        return {"day": None, "setups": [], "skipped": [], "universe": [], "wide": None}
    uni = universe(wide=wide)
    disc = [u for u in uni if u["tier"] == "kesif"]
    if refresh:
        # own list: always. Discovery: only names still missing the base-day bar (S&P 500 bars are kept fresh for breadth).
        have = {r["symbol"] for r in db.query("SELECT symbol FROM prices_daily WHERE day = ? AND close IS NOT NULL", (day,))}
        prices.refresh([u["symbol"] for u in uni if u["tier"] != "kesif" or u["symbol"] not in have])
    spy = prices.series(config.CALENDAR_SYMBOL)
    rscore, rlabel = _regime_score(day)
    start = (date.fromisoformat(day) - timedelta(days=420)).isoformat()
    found, skipped = [], []
    wstat = {"lists": sorted({u["src"] for u in disc}), "n": len(disc), "no_data": 0, "why": {}}
    # a discovery idea still open from an earlier day is not a new idea: keep one row per symbol + kind while it lives
    open_disc = {(r["symbol"], r["kind"]) for r in db.query(
        "SELECT symbol, kind FROM setups WHERE source = ? AND tier = 'kesif' AND day < ? AND valid_until >= ? "
        "AND status IN ('waiting', 'triggered')", (source, day, day))}
    for u in uni:
        is_disc = u["tier"] == "kesif"
        bars = [b for b in prices.bars(u["symbol"], start) if b["close"] is not None and b["day"] <= day]
        if not bars or bars[-1]["day"] != day:
            if is_disc:
                wstat["no_data"] += 1
            else:
                skipped.append({**u, "why": "bu gün için fiyat verisi yok"})
            continue
        a = Arr(bars)
        ss, why = scan_symbol(u["symbol"], a, a.n - 1, spy, rscore, lambda sym=u["symbol"], f=not is_disc: _earn_days(sym, fmp=f))
        if is_disc and ss:
            ss = [s for s in ss if (u["symbol"], s.kind) not in open_disc]
            if not ss:
                wstat["why"]["önceki günden açık kurulum"] = wstat["why"].get("önceki günden açık kurulum", 0) + 1
                continue
        if ss:
            for s in ss:
                found.append((u, s))
        elif is_disc:
            g = _why_group(why)
            wstat["why"][g] = wstat["why"].get(g, 0) + 1
        else:
            skipped.append({**u, "why": why})
    found.sort(key=lambda us: -us[1].quality)
    ids = [store(s, u, source=source, regime=rlabel) for u, s in found]
    if disc and source == "live":
        wstat["found"] = sum(1 for u, s in found if u["tier"] == "kesif" and s.quality >= MIN_QUALITY)
        wstat["picked"] = len(mark_discovery(day))
    return {"day": day, "setups": ids, "skipped": skipped, "universe": uni, "regime": rlabel, "regime_score": rscore,
            "wide": wstat if disc else None}


def pick_discovery(rows: list[dict]) -> list[int]:
    """PURE: ids of the few discovery setups shown on the page: strongest first, quality ≥ DISCOVERY_MIN_QUALITY,
    stop at least DISCOVERY_MIN_STOP_ATR × ATR away, at most DISCOVERY_PER_SECTOR per sector (five names of one
    sector are one bet, not five ideas)."""
    out: list[int] = []
    per: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: (-(r.get("quality") or 0), -(r.get("rr") or 0), r.get("symbol") or "")):
        if (r.get("quality") or 0) < DISCOVERY_MIN_QUALITY or len(out) >= DISCOVERY_CARDS:
            break
        atr = (r.get("ind") or {}).get("atr")
        if atr and r.get("entry") is not None and r.get("stop") is not None and r["entry"] - r["stop"] < DISCOVERY_MIN_STOP_ATR * atr:
            continue
        sec = r.get("sector") or "?"
        if per.get(sec, 0) >= DISCOVERY_PER_SECTOR:
            continue
        per[sec] = per.get(sec, 0) + 1
        out.append(r["id"])
    return out


def _earnings_inside(sym: str, day: str, until: str) -> str | None:
    """Warning text when the next earnings date (Yahoo, cached 12 h) falls inside the setup window. Called only for the
    few picked discovery names: they are not in the radar's own calendar."""
    try:
        from . import stockdata
        rows = stockdata.yahoo("earnings_dates", sym, 12).data or []
    except Exception:  # noqa: BLE001
        return None
    for r in rows:
        d = str(r.get("index") or "")[:10]
        if len(d) == 10 and day < d <= until:
            return (f"Bilanço {tr_day(d)}: setup süresi içinde (Yahoo takvimi). Bilanço öncesi tetiklere güvenme; "
                    "pozisyon bilançoya taşınırsa boşluk riski stop'u atlayabilir.")
    return None


DISCOVERY_RECORD_CURSOR = "setup_discovery_record"


def _outcome(rs: list[dict]) -> dict:
    done = [r["r_mult"] for r in rs if r["r_mult"] is not None]
    hits = sum(1 for r in rs if r["status"] in ("t1", "t2") or r["t1_day"])
    stops = sum(1 for r in rs if r["status"] == "stop" and not r["t1_day"])
    if not done:
        return {"n": 0, "hit": None, "exp_r": None, "ci": None}
    mean = sum(done) / len(done)
    sd = (sum((x - mean) ** 2 for x in done) / len(done)) ** 0.5
    return {"n": len(done), "hit": hits / (hits + stops) * 100 if hits + stops else None, "exp_r": mean, "ci": 1.96 * sd / len(done) ** 0.5}


def discovery_record(save: bool = False) -> dict | None:
    """Backtest of the discovery pick rule itself: what `pick_discovery` would have shown each day and how those ended
    (trades, hit %, expectancy in R with a 95% band), next to every discovery setup above the quality threshold.
    Needs a wide backfill (`setups backfill --wide`); the result is stored so pages do not recompute it."""
    rows = [load(r) for r in db.query("SELECT * FROM setups WHERE source = 'backtest' AND tier = 'kesif'")]
    if not rows:
        return None
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    picks = []
    for rs in by_day.values():
        ids = set(pick_discovery(rs))
        picks += [r for r in rs if r["id"] in ids]
    out = {"from": min(by_day), "to": max(by_day), "days": len(by_day), "symbols": len({r["symbol"] for r in rows}),
           "picks": _outcome(picks), "all": _outcome([r for r in rows if r["quality"] >= MIN_QUALITY]),
           "rules_ver": RULES_VER, "computed_at": utcnow_iso()}
    if save:
        db.set_cursor(DISCOVERY_RECORD_CURSOR, json.dumps(out))
    return out


def mark_discovery(day: str) -> list[int]:
    """Choose the day's discovery picks and store, for each, whether earnings fall inside its window."""
    rows = [load(r) for r in db.query("SELECT * FROM setups WHERE day = ? AND source = 'live' AND tier = 'kesif'", (day,))]
    ids = pick_discovery(rows)
    db.execute("UPDATE setups SET picked = 0 WHERE day = ? AND source = 'live' AND tier = 'kesif'", (day,))
    for r in rows:
        if r["id"] not in ids:
            continue
        warn = _earnings_inside(r["symbol"], r["day"], r["valid_until"])
        raw = db.query_one("SELECT details FROM setups WHERE id = ?", (r["id"],))
        body = json.loads(raw["details"] or "{}") | {"earn_warn": warn}
        db.execute("UPDATE setups SET picked = 1, details = ? WHERE id = ?", (json.dumps(body, ensure_ascii=False, default=str), r["id"]))
    return ids


def store(s: Setup, u: dict, *, source: str, regime: str | None) -> int:
    valid = _add_sessions(s.day, VALID_DAYS[s.kind])
    body = {k: v for k, v in asdict(s).items() if k not in ("symbol", "kind", "day", "entry_lo", "entry_hi", "stop", "t1", "t2", "quality")}
    body.update(rr=s.rr, src=u.get("src"), held=u.get("held"), company=u.get("company"), sector=u.get("sector"))
    db.execute(
        """INSERT INTO setups(symbol, kind, day, source, rules_ver, entry_lo, entry_hi, stop, t1, t2, rr, quality, valid_until,
                              status, regime, details, created_at, tier)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'waiting', ?,?,?,?)
           ON CONFLICT(symbol, kind, day, source) DO UPDATE SET entry_lo=excluded.entry_lo, entry_hi=excluded.entry_hi, stop=excluded.stop,
             t1=excluded.t1, t2=excluded.t2, rr=excluded.rr, quality=excluded.quality, valid_until=excluded.valid_until,
             regime=excluded.regime, details=excluded.details, rules_ver=excluded.rules_ver, tier=excluded.tier""",
        (s.symbol, s.kind, s.day, source, RULES_VER, s.entry_lo, s.entry_hi, s.stop, s.t1, s.t2, s.rr, s.quality, valid,
         regime, json.dumps(body, ensure_ascii=False, default=str), utcnow_iso(), u.get("tier") or "liste"))
    r = db.query_one("SELECT id FROM setups WHERE symbol = ? AND kind = ? AND day = ? AND source = ?", (s.symbol, s.kind, s.day, source))
    return r["id"]


def _add_sessions(day: str, n: int) -> str:
    """Business days after `day` (US holidays not modelled; the trading calendar is used when available)."""
    cal = prices.trading_days(day)
    if len(cal) > n:
        return cal[n]
    d = date.fromisoformat(day)
    k = 0
    while k < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            k += 1
    return d.isoformat()


# ── outcome tracking ──────────────────────────────────────

def follow(row: dict, bars: list[dict]) -> dict:
    """Walk bars after the signal day. Pure function: returns the fields to update.

    Trigger: first close ≥ entry_lo within the validity window (fill = that close, capped at entry_hi... a close
    above the band still counts but fills at the close). Before triggering, a close below the stop invalidates.
    After triggering (from the next bar): low ≤ stop → stop (gap: fill at open); high ≥ t1 → half off, stop → entry;
    high ≥ t2 → done. Same bar touching both stop and target counts as stop (conservative). HOLD_MAX bars → time exit.
    """
    st = {"status": "waiting", "trig_day": None, "trig_px": None, "t1_day": None, "exit_day": None, "exit_px": None,
          "r_mult": None, "mfe": None, "mae": None}
    after = [b for b in bars if b["day"] > row["day"] and b["close"] is not None]
    risk = None
    stop = row["stop"]
    held = 0
    hi_seen = lo_seen = None
    for b in after:
        h = b["high"] if b["high"] is not None else b["close"]
        lo = b["low"] if b["low"] is not None else b["close"]
        o = b["open"] if b["open"] is not None else b["close"]
        if st["status"] == "waiting":
            if b["day"] > row["valid_until"]:
                st["status"] = "expired"
                break
            if b["close"] < row["stop"]:
                st.update(status="invalid", exit_day=b["day"])
                break
            if b["close"] >= row["entry_lo"]:
                st.update(status="triggered", trig_day=b["day"], trig_px=b["close"])
                risk = b["close"] - row["stop"]
                if risk <= 0:
                    st.update(status="invalid", exit_day=b["day"])
                    break
                hi_seen, lo_seen = b["close"], b["close"]
            continue
        held += 1
        hi_seen, lo_seen = max(hi_seen, h), min(lo_seen, lo)
        entry = st["trig_px"]
        if lo <= stop:
            px = min(o, stop)
            if st["status"] == "t1":
                st.update(exit_day=b["day"], exit_px=px, r_mult=round(0.5 * (row["t1"] - entry) / risk + 0.5 * (px - entry) / risk, 3))
            else:
                st.update(status="stop", exit_day=b["day"], exit_px=px, r_mult=round((px - entry) / risk, 3))
            break
        if st["status"] == "triggered" and h >= row["t1"]:
            st.update(status="t1", t1_day=b["day"])
            stop = entry  # breakeven on the second half
        if st["status"] == "t1" and h >= row["t2"]:
            st.update(status="t2", exit_day=b["day"], exit_px=row["t2"],
                      r_mult=round(0.5 * (row["t1"] - entry) / risk + 0.5 * (row["t2"] - entry) / risk, 3))
            break
        if held >= HOLD_MAX:
            px = b["close"]
            r = (0.5 * (row["t1"] - entry) + 0.5 * (px - entry)) / risk if st["status"] == "t1" else (px - entry) / risk
            st.update(exit_day=b["day"], exit_px=px, r_mult=round(r, 3), status=st["status"] if st["status"] == "t1" else "time")
            break
    if risk and hi_seen is not None:
        st["mfe"] = round((hi_seen - st["trig_px"]) / risk, 3)
        st["mae"] = round((lo_seen - st["trig_px"]) / risk, 3)
    if st["status"] == "waiting" and after and after[-1]["day"] >= row["valid_until"] and st["trig_day"] is None:
        st["status"] = "expired"
    return st


def closed(row: dict) -> bool:
    return row["status"] in ("expired", "invalid", "stop", "t2", "time") or (row["status"] == "t1" and row.get("exit_day"))


def update_outcomes(source: str | None = None) -> int:
    rows = db.query("SELECT * FROM setups WHERE (exit_day IS NULL AND status NOT IN ('expired','invalid'))" +
                    (" AND source = ?" if source else ""), (source,) if source else ())
    n = 0
    rows = [dict(r) for r in rows]
    first: dict[str, str] = {}
    for r in rows:
        first[r["symbol"]] = min(first.get(r["symbol"], r["day"]), r["day"])
    cache = {sym: prices.bars(sym, d) for sym, d in first.items()}
    for r in rows:
        bars = cache[r["symbol"]]
        st = follow(r, bars)
        if any(st[k] != r.get(k) for k in st):
            db.execute("UPDATE setups SET status=?, trig_day=?, trig_px=?, t1_day=?, exit_day=?, exit_px=?, r_mult=?, mfe=?, mae=?, "
                       "updated_at=? WHERE id=?",
                       (st["status"], st["trig_day"], st["trig_px"], st["t1_day"], st["exit_day"], st["exit_px"], st["r_mult"],
                        st["mfe"], st["mae"], utcnow_iso(), r["id"]))
            n += 1
            if r["source"] == "live" and st["status"] != r["status"] and r.get("tier") != "kesif":
                _status_alert(r, st)
    return n


ALERT_ON = {"triggered", "t1", "t2", "stop", "time"}


def _status_alert(r: dict, st: dict) -> None:
    """Telegram note when a live setup changes state (trigger, target, stop)."""
    if st["status"] not in ALERT_ON or db.get_setting("setup_alerts") == "0":
        return
    from . import notifier
    icon = {"triggered": "🟡", "t1": "🟢", "t2": "🟢", "stop": "🔴"}.get(st["status"], "⚪")
    name = KINDS.get(r["kind"], (r["kind"],))[0]
    msg = f"{icon} <b>{r['symbol']}</b> {name} ({tr_day(r['day'])}): {STATUS_TR.get(st['status'], st['status'])}"
    if st["status"] == "triggered" and st.get("trig_px"):
        msg += f" — giriş {tr_money(st['trig_px'])}, stop {tr_money(r['stop'])}, H1 {tr_money(r['t1'])}"
    elif st.get("exit_px") and st.get("r_mult") is not None:
        msg += f" — çıkış {tr_money(st['exit_px'])}, sonuç {tr_num(st['r_mult'])}R"
    elif st["status"] == "t1":
        msg += f" — stop başabaşa çekildi, H2 {tr_money(r['t2'])}"
    notifier.enqueue(msg + f"\n🔗 {config.PUBLIC_BASE_URL}/h/{r['symbol']}", kind="setups")


# ── backtest (fills the track record with the same rules) ──

def backfill(days: int = 500, wide: bool = False) -> int:
    """wide=True also replays the discovery tier (index lists): far more samples per setup type, a few minutes of work."""
    uni = universe(wide=wide)
    spy = prices.series(config.CALENDAR_SYMBOL)
    if not spy:
        return 0
    cal = spy.days[-days:]
    regimes = {r["day"]: (r["overall"], r["label"]) for r in db.query("SELECT day, overall, label FROM regime_history")}
    db.execute("DELETE FROM setups WHERE source = 'backtest'")
    n = 0
    for u in uni:
        bars = [b for b in prices.bars(u["symbol"]) if b["close"] is not None]
        if len(bars) < 260:
            continue
        a = Arr(bars)
        earn_cache: list = []

        def earn(sym=u["symbol"], c=earn_cache, f=u["tier"] != "kesif"):
            if not c:
                c.append(_earn_days(sym, fmp=f))
            return c[0]
        idx = {d: k for k, d in enumerate(a.days)}
        last_kind_day: dict[str, int] = {}
        for d in cal[:-1]:
            i = idx.get(d)
            if i is None or i < 200:
                continue
            rs, rl = regimes.get(d, (None, None))
            ss, _ = scan_symbol(u["symbol"], a, i, spy, rs, earn)
            for s in ss:
                # one open idea per symbol+kind: skip re-signals within its validity window
                if i - last_kind_day.get(s.kind, -99) <= VALID_DAYS[s.kind]:
                    continue
                last_kind_day[s.kind] = i
                store(s, u, source="backtest", regime=rl)
                n += 1
    update_outcomes("backtest")
    if wide:
        discovery_record(save=True)
    else:
        db.set_cursor(DISCOVERY_RECORD_CURSOR, "")   # the discovery tier was not replayed: no record to show
    return n


# ── read models ───────────────────────────────────────────

def load(r) -> dict:
    d = dict(r)
    d.update(json.loads(d.pop("details") or "{}"))
    d["name_tr"], d["name_en"] = KINDS[d["kind"]]
    d["status_tr"] = STATUS_TR.get(d["status"], d["status"])
    d["strength"], d["tone"] = strength(d["quality"] or 0)
    d["entry"] = (d["entry_lo"] + d["entry_hi"]) / 2
    return d


def for_day(day: str, source: str = "live") -> list[dict]:
    """Own-list setups of the day (the discovery tier has its own reader)."""
    return [load(r) for r in db.query(f"SELECT * FROM setups WHERE day = ? AND source = ? AND {_OWN} ORDER BY quality DESC", (day, source))]


def discovery_for_day(day: str) -> list[dict]:
    """Discovery-tier setups of the day at or above the quality threshold: picks first, then by quality."""
    return [load(r) for r in db.query("SELECT * FROM setups WHERE day = ? AND source = 'live' AND tier = 'kesif' AND quality >= ? "
                                      "ORDER BY COALESCE(picked, 0) DESC, quality DESC", (day, MIN_QUALITY))]


def open_setups(before: str | None = None) -> list[dict]:
    q = f"SELECT * FROM setups WHERE source = 'live' AND {_VISIBLE} AND (status IN ('waiting','triggered') OR (status = 't1' AND exit_day IS NULL))"
    params: tuple = ()
    if before:
        q += " AND day < ?"
        params = (before,)
    return [load(r) for r in db.query(q + " ORDER BY day DESC, quality DESC", params)]


def recent_closed(limit: int = 30) -> list[dict]:
    return [load(r) for r in db.query(
        f"SELECT * FROM setups WHERE source = 'live' AND {_VISIBLE} AND status NOT IN ('waiting','triggered') AND NOT (status = 't1' AND exit_day IS NULL) "
        "ORDER BY COALESCE(exit_day, valid_until) DESC LIMIT ?", (limit,))]


def track_record(source: str = "live", min_quality: float = 50) -> dict:
    """Only cards that would have been shown (quality ≥ threshold) count."""
    rows = [dict(r) for r in db.query("SELECT kind, status, r_mult, exit_px, trig_px, exit_day, t1_day, day FROM setups "
                                      "WHERE source = ? AND quality >= ?", (source, min_quality))]
    def summarize(rs):
        hits = [r for r in rs if r["status"] in ("t1", "t2") or r["t1_day"]]
        stops = [r for r in rs if r["status"] == "stop" and not r["t1_day"]]
        times = [r for r in rs if r["status"] == "time"]
        expired = [r for r in rs if r["status"] in ("expired", "invalid")]
        done = [r for r in rs if r["r_mult"] is not None]
        wins = [r["r_mult"] for r in done if r["r_mult"] > 0]
        losses = [r["r_mult"] for r in done if r["r_mult"] <= 0]
        pct = lambda r: (r["exit_px"] / r["trig_px"] - 1) * 100 if r["exit_px"] and r["trig_px"] else None  # noqa: E731
        wp = [pct(r) for r in done if r["r_mult"] > 0 and pct(r) is not None]
        lp = [pct(r) for r in done if r["r_mult"] <= 0 and pct(r) is not None]
        decided = len(hits) + len(stops)
        past_window = len(rs) - sum(1 for r in rs if r["status"] == "waiting")  # still-waiting cards have not had their chance yet
        return {"n": len(rs), "decided": decided, "hits": len(hits), "stops": len(stops), "times": len(times), "expired": len(expired),
                "open": len(rs) - len(hits) - len(stops) - len(times) - len(expired),
                "hit": len(hits) / decided * 100 if decided else None,
                "avg_r": sum(r["r_mult"] for r in done) / len(done) if done else None,
                "avg_win": sum(wp) / len(wp) if wp else None, "avg_loss": sum(lp) / len(lp) if lp else None,
                "expectancy_r": (sum(wins) + sum(losses)) / len(done) if done else None,
                "trigger_rate": (past_window - len(expired)) / past_window * 100 if past_window else None}
    by = []
    for k, (tr, en) in KINDS.items():
        s = summarize([r for r in rows if r["kind"] == k])
        if s["n"]:
            by.append({"kind": k, "name": tr, **s})
    by.sort(key=lambda x: -(x["hit"] or -1))
    d30 = (date.today() - timedelta(days=30)).isoformat()
    return {"source": source, "min_quality": min_quality, "all": summarize(rows), "by_kind": by,
            "last30": summarize([r for r in rows if (r["exit_day"] or "") >= d30])}


# ── report ────────────────────────────────────────────────

def macro_warnings(sym: str, day: str, until: str) -> list[str]:
    start = datetime.fromisoformat(day).replace(tzinfo=config.NY_TZ) + timedelta(days=1)
    end = datetime.fromisoformat(until).replace(hour=23, tzinfo=config.NY_TZ)
    rows = db.query("SELECT key, name, release_at, importance FROM macro_calendar WHERE release_at >= ? AND release_at <= ? "
                    "AND (importance >= 4 OR key = ?) ORDER BY release_at",
                    (start.isoformat(), end.isoformat(), f"earnings:{sym}"))
    out = []
    for r in rows:
        when = datetime.fromisoformat(r["release_at"]).astimezone(config.NY_TZ)
        gun = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"][when.weekday()]
        if r["key"] == f"earnings:{sym}":
            out.append(f"Bilanço {tr_day(when.date().isoformat())} {gun}: setup süresi içinde. Bilanço öncesi tetiklere güvenme; pozisyon bilançoya taşınırsa boşluk riski stop'u atlayabilir.")
        elif not r["key"].startswith("earnings:"):
            out.append(f"Makro olay penceresi: {r['name']} {tr_day(when.date().isoformat())} {gun}. Tetik bu güne denk gelirse güvenilirlik düşer.")
    return out[:3]


def option_holdings(sym: str) -> list[dict]:
    from . import portfolio
    try:
        return [p for p in portfolio.positions() if p["asset"] == "option" and p["symbol"].startswith(sym)]
    except Exception:  # noqa: BLE001
        return []


def report(day: str | None = None) -> dict:
    if not day:
        r = db.query_one("SELECT MAX(day) d FROM setup_runs")
        day = (r["d"] if r else None) or base_day()
    cards = for_day(day) if day else []
    for c in cards:
        c["warnings"] = macro_warnings(c["symbol"], c["day"], c["valid_until"])
        c["opt_idea"] = OPTION_IDEA[c["kind"]]
        c["opt_held"] = option_holdings(c["symbol"])
    for c in cards:
        c["ly"] = ladder_y(c)
    passing = [c for c in cards if c["quality"] >= MIN_QUALITY][:MAX_CARDS]
    rest = [c for c in cards if c not in passing]
    disc = discovery_for_day(day) if day else []
    picks = [c for c in disc if c.get("picked")]
    for c in picks:
        c["warnings"] = macro_warnings(c["symbol"], c["day"], c["valid_until"]) + ([c["earn_warn"]] if c.get("earn_warn") else [])
        c["opt_idea"] = OPTION_IDEA[c["kind"]]
        c["opt_held"] = []
        c["ly"] = ladder_y(c)
    meta = db.query_one("SELECT * FROM setup_runs WHERE day = ?", (day,)) if day else None
    meta = dict(meta) if meta else {}
    skipped = json.loads(meta.get("skipped") or "[]")
    rscore, rlabel = _regime_score(day) if day else (None, None)
    down = [x for x in skipped if (x.get("why") or "").startswith("düşüş trendi")]
    for x in down:
        x["why_short"] = x["why"].split(": ", 1)[-1]
    return {"day": day, "cards": passing, "weak": rest, "open": open_setups(before=day), "closed": recent_closed(15),
            "skipped": skipped, "skipped_down": down, "skipped_other": [x for x in skipped if x not in down], "universe_n": meta.get("universe_n") or 0, "summary": meta.get("summary"),
            "summary_model": meta.get("summary_model"), "regime": rlabel, "regime_score": rscore, "ran_at": meta.get("ran_at"),
            "discovery": picks, "discovery_rest": [c for c in disc if not c.get("picked")][:25], "discovery_n": len(disc),
            "wide": json.loads(meta.get("wide") or "null"), "discovery_record": db.loads(db.get_cursor(DISCOVERY_RECORD_CURSOR), None),
            "tr_live": track_record("live"), "tr_bt": track_record("backtest")}


def ladder_y(c: dict, h: float = 150) -> dict:
    """SVG y coordinates (0 = top) for the level ladder on a card."""
    px = (c.get("ind") or {}).get("px") or c["entry_lo"]
    hi, lo = max(c["t2"], px) * 1.01, min(c["stop"], px) * 0.99
    f = lambda v: round((hi - v) / (hi - lo) * h, 2)  # noqa: E731
    return {k: f(c[k]) for k in ("t2", "t1", "entry_hi", "entry_lo", "stop")} | {"px": f(px)}


def template_summary(rep: dict) -> str:
    cards = rep["cards"]
    if not cards:
        return (f"Kendi listende bugün kalite eşiğini geçen setup yok; {rep['universe_n']} isim tarandı. "
                + (f"Piyasa rejimi {rep['regime']}." if rep.get("regime") else ""))
    best = cards[0]
    s = (f"Bugün {len(cards)} setup kalite eşiğini geçti; en güçlüsü {best['symbol']} {best['name_tr'].lower()} "
         f"(R:R 1:{tr_num(best['rr'])}, kalite {best['quality']:.0f}).")
    if rep.get("regime"):
        s += f" Piyasa rejimi {rep['regime']}."
    return s


def run_daily(use_llm: bool = True) -> dict:
    """Update outcomes, scan the last complete session, store the run, queue a Telegram note."""
    from . import notifier
    update_outcomes("live")
    res = scan()
    if not res["day"]:
        return res
    update_outcomes("live")
    rep = report(res["day"])
    rep["universe_n"] = len(res["universe"])
    first_run = db.query_one("SELECT 1 FROM setup_runs WHERE day = ?", (res["day"],)) is None
    summary, model = template_summary(rep), "şablon"
    if use_llm and rep["cards"]:
        summary, model = llm_summary(rep, summary)
    db.execute("INSERT INTO setup_runs(day, ran_at, universe_n, skipped, summary, summary_model, wide) VALUES (?,?,?,?,?,?,?) "
               "ON CONFLICT(day) DO UPDATE SET ran_at=excluded.ran_at, universe_n=excluded.universe_n, skipped=excluded.skipped, "
               "summary=excluded.summary, summary_model=excluded.summary_model, wide=excluded.wide",
               (res["day"], utcnow_iso(), len(res["universe"]), json.dumps(res["skipped"], ensure_ascii=False), summary, model,
                json.dumps(res.get("wide"), ensure_ascii=False) if res.get("wide") else None))
    if first_run and rep["cards"] and db.get_setting("setup_alerts") != "0":  # one Telegram note per session (own list only)
        lines = [f"🎯 <b>Günün setup'ları</b> · {tr_day(res['day'])} kapanışı"]
        for c in rep["cards"]:
            lines.append(f"• <b>{c['symbol']}</b> {c['name_tr']} — giriş {tr_money(c['entry_lo'])}–{tr_money(c['entry_hi'])}, "
                         f"stop {tr_money(c['stop'])}, H1 {tr_money(c['t1'])} · R:R 1:{tr_num(c['rr'])} · {c['strength']}")
        lines.append(f"🔗 {config.PUBLIC_BASE_URL}/setups")
        notifier.enqueue("\n".join(lines), kind="setups")
    return res


def llm_summary(rep: dict, fallback: str) -> tuple[str, str]:
    from . import llm, scorecard
    facts = {"gun": rep["day"], "taranan": rep["universe_n"], "rejim": rep.get("regime"),
             "setuplar": [{"sembol": c["symbol"], "tip": c["name_tr"], "kalite": round(c["quality"]), "rr": round(c["rr"], 1),
                           "giris": round(c["entry_lo"], 2), "stop": round(c["stop"], 2), "hedef1": round(c["t1"], 2),
                           "uyari": c["warnings"][:1]} for c in rep["cards"]]}
    payload = json.dumps(facts, ensure_ascii=False)
    prompt = ("Aşağıdaki günlük setup taramasını Türkçe tek cümleyle (en fazla 40 kelime) özetle ve 'Yani' ile başlayan kısa bir ikinci "
              "cümleyle bitir. Yalnızca verilen sayıları kullan; yeni sayı uydurma; al/sat tavsiyesi verme. Sadece metni yaz.\n\n" + payload)
    try:
        text, model = llm.complete(prompt, timeout=120, budget="daily")
    except llm.LLMError:
        return fallback, "şablon"
    text = " ".join(text.strip().strip('"').split())
    allowed = scorecard._numbers(payload) | {"1", "2", "20", "50", "200"}
    if scorecard._numbers(text) - allowed or not text or len(text) > 500:
        return fallback, "şablon (model metni doğrulanamadı)"
    return text, model
