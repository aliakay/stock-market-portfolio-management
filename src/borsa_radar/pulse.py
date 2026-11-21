"""Piyasa Nabzı: daily market snapshot at one as-of day T.

Index cards (trend, Weinstein-style stage, RSI), macro tiles, realized vs
implied volatility with 1-year percentiles, momentum-factor stress,
distribution days, watchlist/portfolio movers, the macro calendar, Fed-related
prediction markets and the monthly options-expiry countdown.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta

import pandas as pd

from . import config, db, portfolio, prices, technicals
from .collectors import kalshi, macro
from .util import parse_dt

YIELDS = {"^TNX", "^IRX"}


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _chg(s: pd.Series, n: int) -> float | None:
    s = s.dropna()
    if len(s) <= n or not s.iloc[-n - 1]:
        return None
    return float((s.iloc[-1] / s.iloc[-n - 1] - 1) * 100)


def stage(px: float, ma150: float, ma150_prev: float, ma150_long: float) -> tuple[int, str]:
    """Weinstein-style stage from the 30-week (≈150-day) average and its 20-day slope."""
    slope = (ma150 / ma150_prev - 1) * 100
    if px > ma150 and slope > 0.5:
        return 2, "Yükseliş"
    if px < ma150 and slope < -0.5:
        return 4, "Düşüş"
    if ma150 >= ma150_long:
        return 3, "Tepe / dengelenme"
    return 1, "Taban"


def hv_series(closes: pd.Series, n: int) -> pd.Series:
    lr = (closes / closes.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else float("nan"))
    return lr.rolling(n).std() * math.sqrt(252) * 100


def pctile(s: pd.Series) -> float | None:
    s = s.dropna().iloc[-252:]
    if len(s) < 60:
        return None
    return float((s < s.iloc[-1]).sum() / (len(s) - 1) * 100)


def distribution_days(close: pd.Series, vol: pd.Series, n: int = 25) -> int | None:
    df = pd.DataFrame({"c": close, "v": vol}).dropna()
    if len(df) < n + 1:
        return None
    d = df.iloc[-(n + 1):]
    down = (d["c"] / d["c"].shift(1) - 1) <= -0.002
    heavier = d["v"] > d["v"].shift(1)
    return int((down & heavier).iloc[1:].sum())


def third_friday(y: int, m: int) -> date:
    c = calendar.Calendar()
    fridays = [d for d in c.itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fridays[2]


def next_opex(today: date) -> dict:
    d = third_friday(today.year, today.month)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = third_friday(y, m)
    bdays = sum(1 for k in range(1, (d - today).days + 1) if (today + timedelta(days=k)).weekday() < 5)
    return {"date": d.isoformat(), "triple": d.month in (3, 6, 9, 12), "t_minus": bdays}


def index_cards(m, i: int) -> list[dict]:
    out = []
    for sym, name in config.INDEX_ETFS.items():
        if sym not in m.close.columns:
            out.append({"symbol": sym, "name": name, "missing": True})
            continue
        s = m.close[sym].iloc[: i + 1].dropna()
        if len(s) < 200:
            out.append({"symbol": sym, "name": name, "missing": True})
            continue
        xs = list(s)
        px = xs[-1]
        s50, s200 = technicals.sma(xs, 50), technicals.sma(xs, 200)
        ma150, ma150p, ma150l = sum(xs[-150:]) / 150, sum(xs[-170:-20]) / 150, sum(xs[-250:-100]) / 150 if len(xs) >= 250 else sum(xs[:150]) / 150
        st, stl = stage(px, ma150, ma150p, ma150l)
        hi = max(xs[-252:])
        r = technicals.rsi(xs)
        trend = "BOĞA" if px > s200 and s50 > s200 else "AYI" if px < s200 and s50 < s200 else "DİKKAT"
        read = (f"{'50G ve 200G üstünde' if px > s50 and px > s200 else '50G altında, 200G üstünde' if px > s200 else '50G üstünde, 200G altında' if px > s50 else '50G ve 200G altında'}"
                f"; 30 haftalık ortalamaya göre %{(px / ma150 - 1) * 100:+.1f}.")
        out.append({"symbol": sym, "name": name, "price": px, "chg1": technicals.ret(xs, 1), "chg5": technicals.ret(xs, 5),
                    "chg20": technicals.ret(xs, 20), "sma50": s50, "sma200": s200, "hi52": hi, "from_hi": (px / hi - 1) * 100,
                    "rsi": r, "stage": st, "stage_label": stl, "trend": trend, "read": read,
                    "tone": "pos" if trend == "BOĞA" else "neg" if trend == "AYI" else "warn"})
    return out


def macro_tiles(m, i: int) -> list[dict]:
    out = []
    for sym, name in config.MACRO_PROXIES.items():
        if sym not in m.close.columns:
            out.append({"symbol": sym, "name": name, "value": None})
            continue
        s = m.close[sym].iloc[: i + 1].dropna()
        if s.empty:
            out.append({"symbol": sym, "name": name, "value": None})
            continue
        row = {"symbol": sym, "name": name, "value": float(s.iloc[-1]), "yield": sym in YIELDS}
        if sym in YIELDS and len(s) > 20:
            row.update(d1=float((s.iloc[-1] - s.iloc[-2]) * 100), d5=float((s.iloc[-1] - s.iloc[-6]) * 100), d20=float((s.iloc[-1] - s.iloc[-21]) * 100))
        else:
            row.update(d1=_chg(s, 1), d5=_chg(s, 5), d20=_chg(s, 20))
        out.append(row)
    return out


def volatility(m, i: int) -> dict:
    rows = []
    implied = {"SPY": "^VIX", "QQQ": "^VXN"}
    for sym in ("SPY", "QQQ", "SMH"):
        if sym not in m.close.columns:
            continue
        c = m.close[sym].iloc[: i + 1].dropna()
        h10, h20 = hv_series(c, 10), hv_series(c, 20)
        if h20.dropna().empty:
            continue
        r = {"symbol": sym, "hv10": _num(h10.iloc[-1]), "hv20": _num(h20.iloc[-1]), "hv20_5d": _num(h20.iloc[-6]) if len(h20) > 6 else None,
             "hv20_pct": pctile(h20), "hv20_lo": _num(h20.iloc[-252:].min()), "hv20_hi": _num(h20.iloc[-252:].max())}
        iv = implied.get(sym)
        if iv and iv in m.close.columns:
            v = m.close[iv].iloc[: i + 1].dropna()
            r.update(iv_sym=iv.lstrip("^"), iv=_num(v.iloc[-1]), iv_5d=_num(v.iloc[-6]) if len(v) > 6 else None, iv_pct=pctile(v))
        r["hot"] = r["hv10"] is not None and r["hv20"] is not None and r["hv10"] > r["hv20"]
        rows.append(r)
    stress = None
    if {"MTUM", "SPY"} <= set(m.close.columns):
        mt = hv_series(m.adj["MTUM"].iloc[: i + 1].dropna(), 60)
        sp = hv_series(m.adj["SPY"].iloc[: i + 1].dropna(), 60)
        ratio = (mt / sp).dropna()
        vr = None
        if {"^VXN", "^VIX"} <= set(m.close.columns):
            vr = _num((m.close["^VXN"].iloc[i]) / m.close["^VIX"].iloc[i])
        if not ratio.empty:
            rv = float(ratio.iloc[-1])
            stress = {"ratio": rv, "pct": pctile(ratio), "mtum_vol": _num(mt.iloc[-1]), "spy_vol": _num(sp.iloc[-1]), "vxn_vix": vr,
                      "level": "AŞIRI STRES" if rv > 1.9 else "Norm üstü" if rv > 1.6 else "Normal" if rv >= 1.2 else "Sakin"}
    hot = sum(r["hot"] for r in rows)
    result = (f"{len(rows)} fonun {hot}'ünde son 10 gün, son 20 günden sert." if rows else "Oynaklık doğrulanamadı.")
    spy = next((r for r in rows if r["symbol"] == "SPY"), None)
    if spy and spy.get("iv") is not None and spy["hv20"] is not None:
        result += (" Opsiyon piyasası önümüzdeki ayı son 20 günden sakin fiyatlıyor (VIX < HV20)." if spy["iv"] < spy["hv20"]
                   else " Opsiyon piyasası önümüzdeki ayı son 20 günden oynak fiyatlıyor (VIX > HV20).")
    return {"rows": rows, "stress": stress, "result": result}


def movers(day: str) -> list[dict]:
    syms = {p["symbol"] for p in portfolio.positions() if p["asset"] == "stock"} | {t["symbol"] for t in db.active_tickers()}
    held = {p["symbol"] for p in portfolio.positions()}
    out = []
    for s in syms:
        bs = [b for b in prices.bars(s, (date.fromisoformat(day) - timedelta(days=40)).isoformat()) if b["day"] <= day and b["close"]]
        if len(bs) < 6 or bs[-1]["day"] != day:
            continue
        c = [b["close"] for b in bs]
        v = [b["volume"] or 0 for b in bs]
        avgv = sum(v[-21:-1]) / max(1, len(v[-21:-1]))
        out.append({"symbol": s, "price": c[-1], "chg1": (c[-1] / c[-2] - 1) * 100, "chg5": (c[-1] / c[-6] - 1) * 100,
                    "vol_x": v[-1] / avgv if avgv else None, "held": s in held})
    return sorted(out, key=lambda r: -abs(r["chg1"]))


def calendar_rows(days: int = 10) -> list[dict]:
    out = []
    for r in macro.upcoming(24 * days):
        if (r["importance"] or 0) >= 3:
            out.append({"name": r["name"], "key": r["key"], "at": parse_dt(r["release_at"]).astimezone(config.LOCAL_TZ), "importance": r["importance"]})
    return out


def fed_context() -> dict:
    nxt = db.query_one("SELECT release_at FROM macro_calendar WHERE key = 'fomc' AND release_at >= ? ORDER BY release_at LIMIT 1",
                       (datetime.now(config.LOCAL_TZ).astimezone().isoformat()[:10],))
    groups = [g for g in kalshi.overview() if g["series"] in ("KXFEDDECISION", "KXRATECUTCOUNT")]
    return {"next_fomc": parse_dt(nxt["release_at"]).astimezone(config.LOCAL_TZ) if nxt else None, "kalshi": groups[:3]}


def build(m, i: int | None = None) -> dict:
    i = len(m.days) - 1 if i is None else i
    day = m.days[i]
    idx = index_cards(m, i)
    up = [c for c in idx if not c.get("missing") and c["stage"] == 2]
    top = [c for c in idx if not c.get("missing") and c["stage"] == 3]
    dist = {}
    for s in ("SPY", "QQQ"):
        if s in m.close.columns and s in m.volume.columns:
            dist[s] = distribution_days(m.close[s].iloc[: i + 1], m.volume[s].iloc[: i + 1])
    spy = next((c for c in idx if c["symbol"] == "SPY" and not c.get("missing")), None)
    if spy:
        result = (f"S&P 500 (SPY) {spy['price']:.2f}, günlük %{spy['chg1']:+.2f}, 200 günlük ortalamanın %{(spy['price'] / spy['sma200'] - 1) * 100:+.1f} "
                  f"{'üstünde' if spy['price'] > spy['sma200'] else 'altında'}; {len(idx)} endeks fonunun {len(up)}'i yükseliş, {len(top)}'i tepe evresinde.")
    else:
        result = "Endeks verisi doğrulanamadı."
    return {"day": day, "indices": idx, "macro": macro_tiles(m, i), "vol": volatility(m, i), "dist": dist,
            "movers": movers(day), "calendar": calendar_rows(10), "fed": fed_context(),
            "opex": next_opex(date.fromisoformat(day) + timedelta(days=1)), "result": result}


def facts_for_summary(day: str) -> dict:
    """Small fact set the regime summary sentence may use."""
    from . import regime
    m = regime.load()
    if day not in m.days:
        return {}
    i = m.days.index(day)
    p = {c["symbol"]: c for c in index_cards(m, i) if not c.get("missing")}
    out = {k: round(p[k]["chg1"], 2) for k in ("SPY", "QQQ", "IWM") if k in p and p[k]["chg1"] is not None}
    return {"gunluk_degisim_yuzde": out}
