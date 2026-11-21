"""Hisse tarayıcı: a Finviz-style screener over every listed US common stock.

A nightly snapshot (`run`) joins three free sources:
- Sharadar's free `tickers` table (listing, exchange, sector, industry; cached weekly in data/),
- SEC XBRL frames (`frames.load_panel`, the same quarterly figures the idea list uses),
- Yahoo daily bars (`prices.refresh`, ~400 days),
plus next earnings dates from Finnhub's calendar. One row per stock is stored as JSON; the page filters and sorts those
rows in memory (a few thousand rows), so any filter combination answers instantly.

The filters are research tools, not tested signals. Only the four idea-list rules have a measured record (/fikirler);
the page labels presets accordingly.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from . import config, db, frames, ideas
from .util import utcnow_iso

log = logging.getLogger(__name__)

TICKERS_FILE = config.DATA_DIR / "sharadar_tickers.csv.zip"
TICKERS_MAX_AGE_DAYS = 7
EXCHANGES = ("NYSE", "NASDAQ", "NYSEMKT", "BATS")
MIN_PRICE = 1.0
MIN_DVOL = 250_000          # 20-day average dollar volume: below this a stock is left out entirely
HISTORY_DAYS = 400
KEEP_RUNS = 3
EARN_DAYS = 45


# ── field and filter definitions (the page form is generated from these) ──

@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    unit: str = ""          # "%", "$", "M$", "B$", "x", "" (plain)
    step: str = "any"
    hint: str = ""


FIELDS: list[Field] = [
    Field("mcap", "Piyasa değeri", "Genel", "B$", hint="milyar $"),
    Field("price", "Fiyat", "Genel", "$"),
    Field("dvol", "Günlük işlem hacmi", "Genel", "M$", hint="20 günlük ortalama, milyon $"),
    Field("rev_yoy", "Gelir büyümesi", "Büyüme", "%", hint="son çeyrek, geçen yılın aynı çeyreğine göre"),
    Field("ni_yoy", "Net kâr büyümesi", "Büyüme", "%", hint="son çeyrek, yıllık (geçen yıl kârdaysa)"),
    Field("rev_ttm", "Yıllık gelir", "Büyüme", "M$", hint="son dört çeyrek, milyon $"),
    Field("gm", "Brüt marj", "Kârlılık", "%", hint="son çeyrek"),
    Field("om", "Faaliyet marjı", "Kârlılık", "%", hint="son dört çeyrek"),
    Field("nm", "Net marj", "Kârlılık", "%", hint="son dört çeyrek"),
    Field("fscore", "F-skoru (7 test)", "Kârlılık", "", "1", hint="finansal iyileşme testleri, 0–7"),
    Field("q_be", "Başa başa kalan çeyrek", "Kârlılık", "", hint="zarar küçülme hızı sürerse"),
    Field("dil", "Hisse sayısı artışı", "Kârlılık", "%", hint="yıllık seyrelme"),
    Field("ps", "Fiyat / satış", "Değerleme", "x"),
    Field("pe", "Fiyat / kazanç", "Değerleme", "x", hint="yalnız kârlı şirketler"),
    Field("cash_pct", "Nakit / piyasa değeri", "Değerleme", "%"),
    Field("chg1", "Günlük değişim", "Teknik", "%"),
    Field("r1m", "1 ay getiri", "Teknik", "%"),
    Field("r3m", "3 ay getiri", "Teknik", "%"),
    Field("r6m", "6 ay getiri", "Teknik", "%"),
    Field("r12m", "12 ay getiri", "Teknik", "%"),
    Field("rs6", "S&P 500'e göre (6 ay)", "Teknik", "%", hint="puan farkı"),
    Field("rsi", "RSI (14)", "Teknik", "", "1"),
    Field("d50", "50 günlük ortalamaya uzaklık", "Teknik", "%"),
    Field("d200", "200 günlük ortalamaya uzaklık", "Teknik", "%"),
    Field("off_hi", "52 hafta zirvesine uzaklık", "Teknik", "%", hint="0 = zirvede, −30 = zirvenin %30 altında"),
    Field("off_lo", "52 hafta dibinden uzaklık", "Teknik", "%"),
    Field("atr", "Günlük oynaklık (ATR)", "Teknik", "%"),
    Field("relvol", "Göreli hacim", "Teknik", "x", hint="bugünkü hacim / 20 günlük ortalama"),
    Field("beta", "Beta (S&P 500'e göre)", "Risk", "r", hint="son 1 yıl, günlük; 1 = piyasa kadar oynar, 0,5 = yarısı kadar"),
    Field("pcorr", "Portföyümle korelasyon", "Risk", "r",
          hint="son 1 yıl, günlük getiriler, bugünkü ağırlıklarla; 0'a yakın = bağımsız, eksi = ters yönde"),
]
FIELD = {f.key: f for f in FIELDS}
GROUPS = ["Genel", "Büyüme", "Kârlılık", "Değerleme", "Teknik", "Risk"]
BETA_DAYS = 252          # one year of daily returns
BETA_MIN_OBS = 120       # fewer common days → no beta (recent listings)
SORTABLE = set(FIELD) | {"symbol", "earn"}

TREND_TR = {"guclu": "Güçlü trend (fiyat > 50g > 200g)", "yukari": "200 günlük ortalamanın üstünde", "asagi": "200 günlük ortalamanın altında"}
PROFIT_TR = {"kar": "Kârlı (son dört çeyrek)", "zarar": "Zararda (son dört çeyrek)", "daralan": "Zararda ama zarar küçülüyor"}
EARN_TR = {"7": "7 gün içinde", "14": "14 gün içinde", "30": "30 gün içinde"}

# presets: query parameters + what they are; `measured` = the rule has a backtest on /fikirler
PRESETS: dict[str, dict] = {
    "kara_yakin": {"name": "Kâra yaklaşanlar", "q": {"rule": "kara_yakin"}, "sort": "rev_yoy", "measured": True,
                   "what": "Fikir listesinin “Kâra yaklaşan” kuralı: 3–4 çeyrektir zarar, zarar iki çeyrektir küçülüyor, gelir ≥ %15 büyüyor."},
    "ilk_kar": {"name": "İlk kez kâra geçenler", "q": {"rule": "ilk_kar"}, "sort": "rev_yoy", "measured": True,
                "what": "Fikir listesinin “İlk kâr” kuralı: üç çeyrek zarardan sonra ilk kârlı çeyrek, gelir ≥ %10 büyüyor."},
    "canslim": {"name": "Hızlanan büyüme + güçlü trend", "q": {"rev_yoy_min": "25", "ni_yoy_min": "25", "accel": "1", "trend": "yukari", "off_hi_min": "-15"},
                "sort": "rev_yoy", "what": "CANSLIM'in C ve A harflerine benzer: gelir ve kâr ≥ %25 büyüyor, büyüme hızlanıyor, hisse zirvesine yakın ve trendde."},
    "momentum": {"name": "52 hafta zirvesine yakın momentum", "q": {"off_hi_min": "-5", "r6m_min": "20", "rs6_min": "10", "dvol_min": "10"},
                 "sort": "rs6", "what": "Zirvesinin %5 yakınında, 6 ayda ≥ %20 yükselmiş ve S&P 500'ü ≥ 10 puan geçmiş, likit hisseler."},
    "asiri_satis": {"name": "Aşırı satılmış kaliteli", "q": {"rsi_max": "30", "fscore_min": "5", "profit": "kar", "d200_min": "0"},
                    "sort": "rsi", "dir": "asc", "what": "RSI 30'un altına inmiş ama hâlâ 200 günlük ortalamanın üstünde, kârlı ve finansalları iyileşen şirketler."},
    "ucuz_buyuyen": {"name": "Ucuz ve büyüyen", "q": {"ps_max": "3", "rev_yoy_min": "20", "gm_min": "30"}, "sort": "rev_yoy",
                     "what": "Satışlarının 3 katından ucuz, geliri ≥ %20 büyüyen ve brüt marjı ≥ %30 olan şirketler."},
    "deger_kalite": {"name": "Değer + kalite", "q": {"pe_max": "15", "fscore_min": "6", "nm_min": "10"}, "sort": "pe", "dir": "asc",
                     "what": "F/K ≤ 15, net marj ≥ %10 ve F-skoru ≥ 6: ucuz ve finansalı güçlenen kârlı şirketler."},
    "sert_dusen": {"name": "Sert düşen büyük şirketler", "q": {"mcap_min": "10", "off_hi_max": "-30"}, "sort": "off_hi", "dir": "asc",
                   "what": "Piyasa değeri ≥ 10 milyar $ olup zirvesinin %30'dan fazla altına düşmüş şirketler. Ucuz mu, bozuk mu, ayrıca bakılmalı."},
    "hacim": {"name": "Hacim patlaması", "q": {"relvol_min": "3", "dvol_min": "5"}, "sort": "relvol",
              "what": "Bugünkü hacmi 20 günlük ortalamasının en az 3 katı olan likit hisseler: haber ya da büyük alıcı/satıcı izi."},
    "dengeleyen": {"name": "Portföyümü dengeleyen düşük betalılar", "q": {"beta_max": "0,6", "pcorr_max": "0,2", "mcap_min": "10", "profit": "kar"},
                   "sort": "beta", "dir": "asc",
                   "what": "Betası ≤ 0,6, portföyünle korelasyonu ≤ 0,2, piyasa değeri ≥ 10 milyar $ ve kârlı şirketler: "
                           "portföyle birlikte düşme eğilimi düşük olanlar. Geçmiş 1 yılın ölçümü; beta ve korelasyon zamanla değişir."},
    "bilanco": {"name": "Bilançosu yaklaşan büyüyenler", "q": {"earn": "14", "rev_yoy_min": "20"}, "sort": "earn", "dir": "asc",
                "what": "14 gün içinde bilanço açıklayacak, geliri ≥ %20 büyüyen şirketler."},
}


# ── sources ────────────────────────────────────────────────

def _tickers_table() -> dict[str, dict]:
    """Sharadar's free `tickers` table → {yahoo symbol: listing info} for active listed common stocks."""
    fresh = TICKERS_FILE.exists() and time.time() - TICKERS_FILE.stat().st_mtime < TICKERS_MAX_AGE_DAYS * 86400
    key = config.secret("SHARADAR_API_KEY")
    if not fresh and key:
        import requests
        try:
            r = requests.get("https://api.sharadar.com/v1.0/data/tickers", params={"api_key": key, "years": "full"}, timeout=120)
            r.raise_for_status()
            zipfile.ZipFile(io.BytesIO(r.content)).testzip()
            tmp = TICKERS_FILE.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(TICKERS_FILE)
        except Exception as exc:  # noqa: BLE001 - keep the last good file
            log.warning("sharadar tickers download failed: %s", type(exc).__name__)
    if not TICKERS_FILE.exists():
        return {}
    out: dict[str, dict] = {}
    with zipfile.ZipFile(TICKERS_FILE) as z:
        for x in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), "utf-8")):
            if x.get("table") != "SEP" or x.get("isdelisted") != "N" or x.get("exchange") not in EXCHANGES:
                continue
            if "Common Stock" not in (x.get("category") or "") or x.get("siccode") == "6770":     # 6770 = blank-check shells
                continue
            t = x["ticker"]
            if t.endswith((".U", ".W", ".R", ".WS")):
                continue
            m = re.search(r"CIK=(\d+)", x.get("secfilings") or "")
            out[t.replace(".", "-")] = {"name": x.get("name"), "sector": x.get("sector") or None, "industry": x.get("industry") or None,
                                         "exch": x["exchange"], "adr": "ADR" in x["category"] or "Canadian" in x["category"],
                                         "cik": int(m.group(1)) if m else None}
    return out


def _earnings(days: int = EARN_DAYS) -> dict[str, tuple[str, str | None]]:
    """Next report date per symbol from Finnhub's calendar (all companies, a week per call)."""
    import httpx
    key = config.secret("FINNHUB_API_KEY")
    if not key:
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    d0 = date.today()
    for k in range(0, days, 7):
        a, b = d0 + timedelta(days=k), d0 + timedelta(days=min(days, k + 6))
        try:
            r = httpx.get("https://finnhub.io/api/v1/calendar/earnings", timeout=30,
                          params={"from": a.isoformat(), "to": b.isoformat(), "token": key})
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("finnhub calendar %s failed: %s", a, type(exc).__name__)
            continue
        for x in r.json().get("earningsCalendar") or []:
            s, d = (x.get("symbol") or "").replace(".", "-"), x.get("date")
            if s and d and (s not in out or d < out[s][0]):
                out[s] = (d, x.get("hour") or None)
        time.sleep(1.1)
    return out


# ── technical numbers from daily bars ──────────────────────

def _rsi(c: np.ndarray, n: int = 14) -> float | None:
    if len(c) <= n + 1:
        return None
    from .lab import rsi
    v = rsi(c, n)[-1]
    return None if math.isnan(v) else float(v)


def technicals(c: np.ndarray, h: np.ndarray, lo: np.ndarray, v: np.ndarray, days: list[str], spy_r6: float | None) -> dict | None:
    """PURE: price-based fields for one stock (arrays oldest → newest)."""
    n = len(c)
    if n < 30 or not c[-1] or c[-1] <= 0:
        return None
    last = float(c[-1])

    def ret(k):
        return (last / c[-1 - k] - 1) * 100 if n > k and c[-1 - k] > 0 else None

    def dist(k):
        return (last / c[-k:].mean() - 1) * 100 if n >= k else None

    s50, s200 = (c[-50:].mean() if n >= 50 else None), (c[-200:].mean() if n >= 200 else None)
    trend = None
    if s200:
        trend = "guclu" if s50 and last > s50 > s200 else ("yukari" if last > s200 else "asagi")
    w = min(n, 252)
    hi, lo52 = float(h[-w:].max()), float(lo[-w:].min())
    tr = np.maximum(h[1:], c[:-1]) - np.minimum(lo[1:], c[:-1])
    atr = float(tr[-14:].mean() / last * 100) if len(tr) >= 14 else None
    dv = c[-21:-1] * v[-21:-1]
    dvol = float((c[-20:] * v[-20:]).mean())
    relvol = float(v[-1] / v[-21:-1].mean()) if len(dv) == 20 and v[-21:-1].mean() > 0 else None
    y0 = next((i for i, d in enumerate(days) if d[:4] == days[-1][:4]), None)
    ytd = (last / c[y0 - 1] - 1) * 100 if y0 and y0 > 0 and c[y0 - 1] > 0 else None
    r6 = ret(126)
    return {"price": last, "chg1": ret(1), "r1w": ret(5), "r1m": ret(21), "r3m": ret(63), "r6m": r6, "r12m": ret(252), "ytd": ytd,
            "rs6": (r6 - spy_r6) if r6 is not None and spy_r6 is not None else None,
            "d20": dist(20), "d50": dist(50), "d200": dist(200), "trend": trend, "rsi": _rsi(c),
            "off_hi": (last / hi - 1) * 100 if hi > 0 else None, "off_lo": (last / lo52 - 1) * 100 if lo52 > 0 else None,
            "atr": atr, "dvol": dvol, "relvol": relvol, "day": days[-1]}


def daily_returns(days: list[str], closes: np.ndarray, cal: list[str]) -> np.ndarray:
    """Close-to-close returns on the calendar `cal` (len(cal) − 1 values); NaN where either day has no close."""
    m = dict(zip(days, closes))
    c = np.array([m.get(d, np.nan) for d in cal], dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return c[1:] / c[:-1] - 1


def beta_corr(x: np.ndarray, mkt: np.ndarray, port: np.ndarray | None) -> tuple[float | None, float | None]:
    """PURE: beta of returns `x` to `mkt` and correlation of `x` with `port` (same calendar, NaN = missing day)."""
    ok = ~np.isnan(x) & ~np.isnan(mkt)
    if ok.sum() < BETA_MIN_OBS or mkt[ok].var() == 0:
        return None, None
    beta = float(np.cov(x[ok], mkt[ok])[0, 1] / mkt[ok].var(ddof=1))
    corr = None
    if port is not None:
        ok2 = ok & ~np.isnan(port)
        if ok2.sum() >= BETA_MIN_OBS and x[ok2].std() > 0 and port[ok2].std() > 0:
            corr = float(np.corrcoef(x[ok2], port[ok2])[0, 1])
    return beta, corr


def _portfolio_returns(cal: list[str], bars: dict) -> tuple[np.ndarray | None, dict]:
    """Daily returns of today's stock holdings at today's weights (what the portfolio *is* now, not what it was)."""
    try:
        from . import portfolio
        pos = [p for p in portfolio.positions() if p.get("asset") == "stock" and (p.get("value") or 0) > 1]
    except Exception:  # noqa: BLE001 - no portfolio → no correlation column
        log.exception("portfolio for screener correlation failed")
        return None, {}
    if not pos:
        return None, {}
    missing = [p["symbol"] for p in pos if p["symbol"] not in bars]
    if missing:
        bars = {**bars, **_bars_all(missing, cal[0])}
    total = sum(p["value"] for p in pos)
    acc = np.zeros(len(cal) - 1)
    for p in pos:
        b = bars.get(p["symbol"])
        if b:
            acc += p["value"] / total * np.nan_to_num(daily_returns(b[0], b[1], cal))
    return acc, {"n": len(pos), "symbols": [p["symbol"] for p in sorted(pos, key=lambda p: -p["value"])]}


def _bars_all(symbols: list[str], start: str) -> dict[str, tuple]:
    """One query for every symbol's bars since `start` → {sym: (days, c, h, l, v)}."""
    out: dict[str, list] = {}
    want = set(symbols)
    for r in db.query("SELECT symbol, day, high, low, close, volume FROM prices_daily WHERE day >= ? AND close IS NOT NULL ORDER BY symbol, day",
                      (start,)):
        if r["symbol"] in want:
            out.setdefault(r["symbol"], []).append((r["day"], r["close"], r["high"] or r["close"], r["low"] or r["close"], r["volume"] or 0.0))
    return {s: ([x[0] for x in rows], np.array([x[1] for x in rows], dtype=float), np.array([x[2] for x in rows], dtype=float),
                np.array([x[3] for x in rows], dtype=float), np.array([x[4] for x in rows], dtype=float)) for s, rows in out.items()}


def _num(x, nd: int = 4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


# ── Yahoo fallback for quarters SEC's XBRL data lacks ─────

YAHOO_TTL_H = 24 * 7          # quarterly statements change four times a year


def yahoo_fund(recs: list[dict] | None, today: date) -> dict | None:
    """PURE: the screener's financial fields from Yahoo quarterly income statements (`stockdata.yahoo` records), for
    companies whose latest quarters are missing from SEC's XBRL data (in 2026: several utilities, banks and REITs,
    Booking, Costco's 12/16-week quarters). Year sums need four quarters that follow each other (≤ 120 days apart),
    year-on-year needs a quarter ending 350–380 days earlier, and the newest must be under 200 days old. There is no
    balance sheet here, so F-score, dilution, cash and the idea rules stay empty."""
    def series(*names) -> dict[str, float]:
        for name in names:
            row = next((r for r in recs or [] if r.get("index") == name), None)
            s = {k[:10]: float(v) for k, v in (row or {}).items()
                 if k != "index" and isinstance(v, (int, float)) and not math.isnan(v)}
            if s:
                return s
        return {}

    rev, ni = series("Total Revenue", "Operating Revenue"), series("Net Income", "Net Income Common Stockholders")
    days = sorted(set(rev) & set(ni), reverse=True)
    if not days or (today - date.fromisoformat(days[0])).days > 200:
        return None
    d = [date.fromisoformat(x) for x in days]
    run = 1                                   # how many newest quarters follow each other without a hole
    while run < len(d) and (d[run - 1] - d[run]).days <= 120:
        run += 1

    def yoy(s: dict, i: int) -> float | None:
        k = next((k for k in range(i + 1, len(d)) if 350 <= (d[i] - d[k]).days <= 380), None)
        a, b = (s.get(days[i]), s.get(days[k])) if k is not None else (None, None)
        return (a / b - 1) * 100 if a is not None and b and b > 0 else None

    def m(x):
        return _num(x / 1e6, 1) if x is not None else None

    r, n = [rev[x] for x in days], [ni[x] for x in days]
    op, gp = series("Operating Income"), series("Gross Profit")
    sh = series("Diluted Average Shares", "Basic Average Shares")
    full = run >= 4
    rev_ttm, ni_ttm = (sum(r[:4]), sum(n[:4])) if full else (None, None)
    op_ttm = sum(op[x] for x in days[:4]) if full and all(x in op for x in days[:4]) else None
    rev_yoy, rev_yoy_prev = yoy(rev, 0), (yoy(rev, 1) if run >= 2 else None)
    slope = (n[0] - n[3]) / 3 if full else None
    y, q = frames._qidx(days[0])
    return {
        "src": "yahoo", "quarter": ideas._fmt_q(f"CY{y}Q{q}"),
        "rev_ttm": m(rev_ttm), "rev_yoy": _num(rev_yoy, 1), "rev_yoy_prev": _num(rev_yoy_prev, 1),
        "ni_ttm": m(ni_ttm), "ni_q0": m(n[0]), "ni_yoy": _num(yoy(ni, 0), 1),
        "nm": _num(ni_ttm / rev_ttm * 100, 1) if rev_ttm and rev_ttm > 0 else None,
        "om": _num(op_ttm / rev_ttm * 100, 1) if op_ttm is not None and rev_ttm and rev_ttm > 0 else None,
        "gm": _num(gp[days[0]] / r[0] * 100, 1) if days[0] in gp and r[0] > 0 else None,
        "fscore": None, "dil": None, "cash": None, "rules": [],
        "q_be": _num(-n[0] / slope, 1) if slope and n[0] < 0 and slope > 0 else None,
        "losses4": sum(1 for x in n[:4] if x < 0) if full else None,
        "narrowing": run >= 3 and n[0] < 0 and n[0] > n[1] > n[2],
        "accel": rev_yoy is not None and rev_yoy_prev is not None and rev_yoy > rev_yoy_prev,
        "shares": _num(sh.get(days[0]), 0),
    }


# ── nightly snapshot ───────────────────────────────────────

def run(refresh_prices: bool = True, fill_yahoo: bool = True, progress=None) -> dict:
    """Prices for every listed stock, SEC numbers from the frames panel, Yahoo quarterly statements where SEC lacks a
    full year of quarters (cached a week), then one stored snapshot the page and CLI filter in memory."""
    from . import prices
    from . import stockdata as sd
    t0 = time.time()
    listing = _tickers_table()
    p = frames.load_panel()
    fund: dict[int, dict] = {}          # by SEC CIK: every share class of a company finds its numbers
    sec_cik: dict[str, int] = {}
    if p is not None:
        aj = ideas.as_of_index(p, date.today(), getattr(p, "derived", None), live=True)
        m = ideas.metrics(p, aj)
        masks = ideas.rules(m)
        with np.errstate(invalid="ignore", divide="ignore"):
            op_ttm = ideas._ttm(p["op_income"], aj)
        sh_ff = m["shares"].copy()          # shares: the latest reported value of the last four quarters
        for k in range(1, 4):
            sh_ff = np.where(np.isnan(sh_ff), ideas._lag(p["shares"], aj, k), sh_ff)
        for cik, (t, _) in p.names.items():
            if t:
                sec_cik[t.replace(".", "-")] = int(cik)
        for i in np.flatnonzero(aj >= 0):
            cik = int(p.ciks[i])
            name = p.names.get(cik, (None, None))[1]
            ni = [m["ni"][k][i] for k in range(5)]
            rev_ttm, ni_ttm = m["rev_ttm"][i], m["ni_ttm"][i]
            with np.errstate(invalid="ignore", divide="ignore"):
                fund[cik] = {
                    "name_sec": name, "quarter": ideas._fmt_q(p.q[int(aj[i])]),
                    "rev_ttm": _num(rev_ttm / 1e6, 1), "rev_yoy": _num(m["rev_yoy"][i] * 100, 1), "rev_yoy_prev": _num(m["rev_yoy_prev"][i] * 100, 1),
                    "ni_ttm": _num(ni_ttm / 1e6, 1), "ni_q0": _num(ni[0] / 1e6, 1), "ni_yoy": _num(m["ni_yoy"][i] * 100, 1),
                    "nm": _num(ni_ttm / rev_ttm * 100, 1) if rev_ttm and rev_ttm > 0 else None,
                    "om": _num(op_ttm[i] / rev_ttm * 100, 1) if rev_ttm and rev_ttm > 0 else None,
                    "gm": _num(m["gm"][i] * 100, 1), "fscore": int(m["fscore"][i]) if m["f_known"][i] >= 5 else None,
                    "dil": _num(m["dil"][i] * 100, 1), "q_be": _num(m["q_be"][i], 1), "losses4": int(m["losses4"][i]),
                    "narrowing": bool(ni[0] < 0 and ni[0] > ni[1] > ni[2]) if not any(math.isnan(x) for x in ni[:3]) else False,
                    "accel": bool(m["rev_yoy"][i] > m["rev_yoy_prev"][i]) if not (math.isnan(m["rev_yoy"][i]) or math.isnan(m["rev_yoy_prev"][i])) else False,
                    "cash": _num(m["cash"][i] / 1e6, 1), "shares": _num(sh_ff[i], 0),
                    "rules": [code for code, mk in masks.items() if mk[i]],
                }
    universe = sorted(listing or sec_cik)
    if progress:
        progress(f"evren {len(universe)} hisse (Sharadar listesi {len(listing)}, SEC finansalı {len(fund)})")
    start = date.today() - timedelta(days=HISTORY_DAYS)
    if refresh_prices:
        prices.refresh(universe + [config.CALENDAR_SYMBOL], start=start)
    bars = _bars_all(universe + [config.CALENDAR_SYMBOL], start.isoformat())
    spy = bars.get(config.CALENDAR_SYMBOL)
    spy_r6 = (spy[1][-1] / spy[1][-127] - 1) * 100 if spy and len(spy[1]) > 127 else None
    cal = spy[0][-(BETA_DAYS + 1):] if spy else []
    mkt = daily_returns(spy[0], spy[1], cal) if len(cal) > BETA_MIN_OBS else None
    port, port_info = _portfolio_returns(cal, bars) if mkt is not None else (None, {})
    earn = _earnings()
    rows: list[dict] = []
    newest = max((b[0][-1] for b in bars.values() if b[0]), default=None)
    stale = (date.fromisoformat(newest) - timedelta(days=10)).isoformat() if newest else None
    n_yahoo = 0
    for s in universe:
        b = bars.get(s)
        if not b or (stale and b[0][-1] < stale):
            continue
        tech = technicals(b[1], b[2], b[3], b[4], b[0], spy_r6)
        if not tech or tech["price"] < MIN_PRICE or tech["dvol"] < MIN_DVOL:
            continue
        lst = listing.get(s, {})
        f = fund.get(lst.get("cik") or sec_cik.get(s) or -1, {})
        # Yahoo where SEC lacks a full year of quarters, or the share count (several share classes: META, GOOGL, V, BRK-B …);
        # never for ADRs, which report in their home currency
        need_fund, need_sh = f.get("rev_ttm") is None, not f.get("shares")
        if fill_yahoo and (need_fund or need_sh) and not lst.get("adr"):
            y = yahoo_fund(sd.yahoo("quarterly_income_stmt", s, YAHOO_TTL_H).data, date.today())
            if y and need_fund and y["rev_ttm"] is not None:
                f = {**f, **{k: v for k, v in y.items() if v is not None or k not in f}}
                n_yahoo += 1
                if progress and n_yahoo % 100 == 0:
                    progress(f"Yahoo çeyrek tabloları: {n_yahoo} şirket")
            elif y and y.get("shares"):
                f = {**f, "shares": y["shares"], "shares_src": "yahoo"}
        r = {"symbol": s, "name": lst.get("name") or f.get("name_sec") or s, "sector": lst.get("sector"), "industry": lst.get("industry"),
             "exch": lst.get("exch"), "adr": lst.get("adr", False), **{k: v for k, v in f.items() if k != "name_sec"},
             **{k: (_num(v, 3) if isinstance(v, float) else v) for k, v in tech.items()}}
        r["dvol"] = _num(tech["dvol"] / 1e6, 2)
        if mkt is not None:
            beta, corr = beta_corr(daily_returns(b[0], b[1], cal), mkt, port)
            r["beta"], r["pcorr"] = _num(beta, 2), _num(corr, 2)
        sh = f.get("shares")
        r["mcap"] = _num(tech["price"] * sh / 1e9, 3) if sh else None
        if r["mcap"] and f.get("rev_ttm") and f["rev_ttm"] > 0:
            r["ps"] = _num(r["mcap"] * 1e3 / f["rev_ttm"], 2)
        if r["mcap"] and f.get("ni_ttm") and f["ni_ttm"] > 0:
            r["pe"] = _num(r["mcap"] * 1e3 / f["ni_ttm"], 1)
        if r["mcap"] and f.get("cash") is not None:
            r["cash_pct"] = _num(f["cash"] / (r["mcap"] * 1e3) * 100, 1)
        if s in earn:
            r["earn"], r["earn_t"] = earn[s]
        rows.append(r)
    run_row = {"created_at": utcnow_iso(), "day": date.today().isoformat(), "price_day": newest, "n": len(rows),
               "universe": len(universe), "with_fund": sum(1 for r in rows if r.get("rev_ttm") is not None), "with_yahoo": n_yahoo,
               "port": {**port_info, "day": cal[-1]} if port_info else None,
               "with_earn": sum(1 for r in rows if r.get("earn")), "seconds": round(time.time() - t0)}
    rid = db.execute("INSERT INTO screener_runs(created_at, day, body) VALUES (?,?,?)", (run_row["created_at"], run_row["day"], json.dumps(run_row)))
    with db.tx() as c:
        c.executemany("INSERT INTO screener_rows(run_id, symbol, body) VALUES (?,?,?)",
                      [(rid, r["symbol"], json.dumps(r, ensure_ascii=False, separators=(",", ":"))) for r in rows])
        old = [x[0] for x in c.execute("SELECT id FROM screener_runs ORDER BY id DESC LIMIT -1 OFFSET ?", (KEEP_RUNS,))]
        for o in old:
            c.execute("DELETE FROM screener_rows WHERE run_id = ?", (o,))
            c.execute("DELETE FROM screener_runs WHERE id = ?", (o,))
    _cache.clear()
    return {**run_row, "id": rid}


# ── reading and filtering ──────────────────────────────────

_cache: dict[int, tuple[dict, list[dict]]] = {}


def latest() -> tuple[dict | None, list[dict]]:
    r = db.query_one("SELECT id, body FROM screener_runs ORDER BY id DESC LIMIT 1")
    if not r:
        return None, []
    if r["id"] not in _cache:
        rows = [json.loads(x["body"]) for x in db.query("SELECT body FROM screener_rows WHERE run_id = ?", (r["id"],))]
        _cache.clear()
        _cache[r["id"]] = (json.loads(r["body"]) | {"id": r["id"]}, rows)
    return _cache[r["id"]]


def _f(params: dict, k: str) -> float | None:
    v = (params.get(k) or "").strip().replace(",", ".")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def apply(rows: list[dict], params: dict) -> list[dict]:
    """PURE: filter rows with the query parameters of the page (`<field>_min`, `<field>_max`, choices)."""
    out = rows
    for f in FIELDS:
        lo, hi = _f(params, f.key + "_min"), _f(params, f.key + "_max")
        if lo is not None:
            out = [r for r in out if r.get(f.key) is not None and r[f.key] >= lo]
        if hi is not None:
            out = [r for r in out if r.get(f.key) is not None and r[f.key] <= hi]
    q = (params.get("q") or "").strip().lower()
    if q:
        out = [r for r in out if q in r["symbol"].lower() or q in (r.get("name") or "").lower() or q in (r.get("industry") or "").lower()]
    if params.get("sector"):
        out = [r for r in out if r.get("sector") == params["sector"]]
    if params.get("trend") == "guclu":
        out = [r for r in out if r.get("trend") == "guclu"]
    elif params.get("trend") == "yukari":
        out = [r for r in out if r.get("trend") in ("guclu", "yukari")]
    elif params.get("trend") == "asagi":
        out = [r for r in out if r.get("trend") == "asagi"]
    pr = params.get("profit")
    if pr == "kar":
        out = [r for r in out if (r.get("ni_ttm") or 0) > 0]
    elif pr == "zarar":
        out = [r for r in out if r.get("ni_ttm") is not None and r["ni_ttm"] < 0]
    elif pr == "daralan":
        out = [r for r in out if r.get("narrowing")]
    if params.get("accel"):
        out = [r for r in out if r.get("accel")]
    if params.get("noadr"):
        out = [r for r in out if not r.get("adr")]
    if params.get("rule"):
        out = [r for r in out if params["rule"] in (r.get("rules") or [])]
    if params.get("earn") in EARN_TR:
        until = (date.today() + timedelta(days=int(params["earn"]))).isoformat()
        out = [r for r in out if r.get("earn") and r["earn"] <= until]
    return out


def sort(rows: list[dict], key: str | None, desc: bool = True) -> list[dict]:
    if key not in SORTABLE:
        key = "mcap"
    have = [r for r in rows if r.get(key) is not None]
    rest = [r for r in rows if r.get(key) is None]
    return sorted(have, key=lambda r: r[key], reverse=desc) + rest


def resolve(params: dict) -> tuple[dict, str, bool, dict | None]:
    """Preset (if any) merged under the explicit parameters → (filter params, sort key, descending, preset)."""
    pre = PRESETS.get(params.get("preset") or "")
    merged = dict(pre["q"]) if pre else {}
    merged.update({k: v for k, v in params.items() if v not in (None, "") and k != "preset"})
    key = params.get("sort") or (pre or {}).get("sort") or "mcap"
    d = params.get("dir") or (pre or {}).get("dir") or "desc"
    return merged, key, d != "asc", pre


def to_csv(rows: list[dict]) -> str:
    cols = ["symbol", "name", "sector", "industry", "price", "mcap", "dvol", "rev_ttm", "rev_yoy", "ni_yoy", "gm", "om", "nm", "fscore",
            "ps", "pe", "cash_pct", "dil", "q_be", "chg1", "r1m", "r3m", "r6m", "r12m", "rs6", "rsi", "d50", "d200", "off_hi", "atr",
            "relvol", "beta", "pcorr", "trend", "earn", "quarter", "src", "rules"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([" ".join(r.get(c) or []) if c == "rules" else ("" if r.get(c) is None else r.get(c)) for c in cols])
    return buf.getvalue()


# ── saved screens ──────────────────────────────────────────

def saved() -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM screener_saved ORDER BY name")]


def save(name: str, query: str) -> None:
    name = name.strip()[:60]
    if name:
        db.execute("INSERT INTO screener_saved(name, query, created_at) VALUES (?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET query = excluded.query, created_at = excluded.created_at",
                   (name, query[:2000], utcnow_iso()))


def delete(sid: int) -> None:
    db.execute("DELETE FROM screener_saved WHERE id = ?", (sid,))


def age_days(run: dict | None) -> int | None:
    if not run:
        return None
    return (datetime.now().date() - date.fromisoformat(run["day"])).days
