"""Price-based indicators computed from the shared daily bar store.

Pure functions over lists so the regime / setup modules can reuse them.
`snapshot(symbol)` is the per-stock summary the hub and scorecard read.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from . import config, prices


def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs: list[float], n: int) -> list[float]:
    if not xs:
        return []
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi(xs: list[float], n: int = 14) -> float | None:
    """Wilder RSI."""
    if len(xs) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(xs[:n], xs[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(xs[n:], xs[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = []
    for prev, b in zip(bars, bars[1:]):
        if None in (b["high"], b["low"], prev["close"]):
            continue
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev["close"]), abs(b["low"] - prev["close"])))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def hv(xs: list[float], n: int = 20) -> float | None:
    """Annualized close-to-close historical volatility (fraction)."""
    if len(xs) <= n:
        return None
    rets = [math.log(b / a) for a, b in zip(xs[-n - 1:], xs[-n:]) if a and b]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def ret(xs: list[float], n: int) -> float | None:
    if len(xs) <= n or not xs[-n - 1]:
        return None
    return (xs[-1] / xs[-n - 1] - 1) * 100


def max_drawdown(xs: list[float]) -> float | None:
    if not xs:
        return None
    peak, mdd = xs[0], 0.0
    for x in xs:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return mdd * 100


def ensure_bars(symbol: str, min_days: int = 260) -> list[dict]:
    """Bars for ~2y; fetches from Yahoo if the store is empty/thin/stale."""
    start = (date.today() - timedelta(days=760)).isoformat()
    bs = prices.bars(symbol, start)
    stale = not bs or bs[-1]["day"] < (date.today() - timedelta(days=4)).isoformat()
    if len(bs) < min_days or stale:
        prices.refresh([symbol, config.CALENDAR_SYMBOL], start=date.fromisoformat(start) if len(bs) < min_days else None)
        bs = prices.bars(symbol, start)
    return [b for b in bs if b["close"] is not None]


def snapshot(symbol: str) -> dict | None:
    bs = ensure_bars(symbol)
    if len(bs) < 30:
        return None
    closes = [b["close"] for b in bs]
    vols = [b["volume"] or 0 for b in bs]
    px = closes[-1]
    last252 = closes[-252:]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    e21 = ema_series(closes, 21)[-1]
    a14 = atr(bs, 14)
    spy = [b["close"] for b in prices.bars(config.CALENDAR_SYMBOL, bs[0]["day"]) if b["close"]]
    rs63 = (ret(closes, 63) - ret(spy, 63)) if ret(closes, 63) is not None and ret(spy, 63) is not None else None
    hi52, lo52 = max(last252), min(last252)
    v20 = sma(vols, 20)
    # swing levels: lowest low / highest high over the last 20 and 60 sessions
    lows20 = min(b["low"] or b["close"] for b in bs[-20:])
    highs20 = max(b["high"] or b["close"] for b in bs[-20:])
    snap = {
        "symbol": symbol.upper(), "as_of": bs[-1]["day"], "price": px, "source": "Yahoo günlük kapanış",
        "chg_1d": ret(closes, 1), "chg_5d": ret(closes, 5), "chg_20d": ret(closes, 20), "chg_63d": ret(closes, 63),
        "chg_252d": ret(closes, 252),
        "sma20": s20, "sma50": s50, "sma200": s200, "ema21": e21,
        "above20": s20 is not None and px > s20, "above50": s50 is not None and px > s50,
        "above200": s200 is not None and px > s200,
        "golden": s50 is not None and s200 is not None and s50 > s200,
        "rsi14": rsi(closes, 14), "atr14": a14, "atr_pct": a14 / px * 100 if a14 else None,
        "hv20": hv(closes, 20), "hv60": hv(closes, 60),
        "hi52": hi52, "lo52": lo52, "from_hi52": (px / hi52 - 1) * 100, "from_lo52": (px / lo52 - 1) * 100,
        "rs63_vs_spy": rs63, "vol_ratio": (vols[-1] / v20) if v20 else None,
        "support20": lows20, "resist20": highs20, "mdd_1y": max_drawdown(last252),
    }
    snap["trend_score"] = trend_score(snap)
    snap["trend_label"] = trend_label(snap)
    return snap


def trend_score(s: dict) -> float:
    """0-100: MA stack, momentum, relative strength, RSI sanity, distance from high."""
    pts = 0.0
    pts += 15 if s["above20"] else 0
    pts += 15 if s["above50"] else 0
    pts += 20 if s["above200"] else 0
    pts += 10 if s["golden"] else 0
    if s["chg_63d"] is not None:
        pts += max(0, min(15, 7.5 + s["chg_63d"] / 2))
    if s["rs63_vs_spy"] is not None:
        pts += max(0, min(15, 7.5 + s["rs63_vs_spy"] / 2))
    r = s["rsi14"]
    if r is not None:
        pts += 10 if 45 <= r <= 70 else 5 if 35 <= r < 45 or 70 < r <= 80 else 0
    return round(pts, 1)


def trend_label(s: dict) -> str:
    if s["above20"] and s["above50"] and s["above200"]:
        return "Güçlü yükseliş trendi" if s["golden"] else "Toparlanan trend"
    if s["above200"] and not s["above50"]:
        return "Uzun vadede yukarı, kısa vadede düzeltme"
    if not s["above200"] and s["above50"]:
        return "Dipten dönüş denemesi"
    return "Düşüş trendi"
