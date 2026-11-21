"""Sector / factor rotation vs SPY.

Relative strength over 1/4/12/26 weeks, an RRG-style quadrant from weekly
data (RS-Ratio = RS ÷ its 10-week average × 100; RS-Momentum = RS-Ratio ÷ its
own 4-week average × 100 — a public approximation of the JdK method, not the
licensed formula), this week's quadrant changes, a six-ratio risk-mode
thermometer, and where the user's own names sit.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from . import config, db, portfolio, stockdata

log = logging.getLogger(__name__)
QUADS = {"Lider": "pos", "Zayıflayan": "warn", "Gecikmeli": "neg", "Toparlanan": "acc"}
RISK_RATIOS = [  # (num, den, title, what, inverted)
    ("XLK", "XLP", "Teknoloji / Temel tüketim", "risk iştahı", False),
    ("SMH", "SPY", "Yarı iletken / S&P 500", "yarı iletken liderliği", False),
    ("HYG", "TLT", "Yüksek getirili / Hazine", "kredi risk iştahı", False),
    ("GLD", None, "Altın", "güvenli liman talebi (yükselişi risk-off)", True),
    ("XLY", "XLP", "İhtiyari / Temel tüketim", "büyüme vs koruma", False),
    ("SPHB", "SPLV", "Yüksek beta / Düşük volatilite", "oynak hisse iştahı", False),
]


# ── math (pure) ───────────────────────────────────────────

def quadrant(ratio: float, mom: float) -> str:
    if ratio >= 100:
        return "Lider" if mom >= 100 else "Zayıflayan"
    return "Toparlanan" if mom >= 100 else "Gecikmeli"


def rrg(rs_weekly: pd.Series, n_ratio: int = 10, n_mom: int = 4) -> pd.DataFrame:
    """rs_weekly: ETF/SPY sampled weekly. Returns columns ratio, mom (NaN until enough history)."""
    ratio = rs_weekly / rs_weekly.rolling(n_ratio).mean() * 100
    mom = ratio / ratio.rolling(n_mom).mean() * 100
    return pd.DataFrame({"ratio": ratio, "mom": mom}).dropna()


def weekly(s: pd.Series) -> pd.Series:
    """Last value of each ISO week (index = day strings)."""
    idx = pd.to_datetime(s.index)
    keys = [f"{d.isocalendar()[0]}-{d.isocalendar()[1]:02d}" for d in idx]
    return s.groupby(keys).tail(1)


def band_pos(s: pd.Series) -> float | None:
    """Where the last value sits in its 1-year range (0-100)."""
    last = s.iloc[-252:]
    lo, hi = last.min(), last.max()
    return float((s.iloc[-1] - lo) / (hi - lo) * 100) if hi > lo else None


# ── report ────────────────────────────────────────────────

def _pct(s: pd.Series, n: int) -> float | None:
    s = s.dropna()
    if len(s) <= n or not s.iloc[-n - 1]:
        return None
    return float((s.iloc[-1] / s.iloc[-n - 1] - 1) * 100)


def report(m, i: int | None = None) -> dict:
    """m: regime.Market. Uses dividend-adjusted closes up to T = m.days[i]."""
    i = len(m.days) - 1 if i is None else i
    day = m.days[i]
    adj = m.adj.iloc[: i + 1]
    if "SPY" not in adj.columns:
        return {"day": day, "rows": [], "missing": ["SPY"]}
    spy = adj["SPY"].dropna()
    rows, missing = [], []
    universe = [(s, n, "Sektör") for s, n in config.SECTOR_ETFS.items()] + [(s, n, "Faktör / tema") for s, n in config.FACTOR_ETFS.items()]
    for sym, name, group in universe:
        if sym not in adj.columns or adj[sym].dropna().shape[0] < 140:
            missing.append(sym)
            continue
        px = adj[sym].dropna()
        rs = (px / spy).dropna()
        w = weekly(rs)
        r = rrg(w)
        if len(r) < 2:
            missing.append(sym)
            continue
        q_now = quadrant(r["ratio"].iloc[-1], r["mom"].iloc[-1])
        q_prev = quadrant(r["ratio"].iloc[-2], r["mom"].iloc[-2])
        rows.append({
            "symbol": sym, "name": name, "group": group,
            "ret": {n: _pct(px, d) for n, d in (("1h", 5), ("4h", 20), ("12h", 60), ("26h", 130))},
            "rel": {n: (_pct(rs, d)) for n, d in (("1h", 5), ("4h", 20), ("12h", 60), ("26h", 130))},
            "ratio": float(r["ratio"].iloc[-1]), "mom": float(r["mom"].iloc[-1]),
            "quad": q_now, "prev_quad": q_prev, "changed": q_now != q_prev,
            "tail": [{"ratio": float(a), "mom": float(b)} for a, b in zip(r["ratio"].iloc[-6:], r["mom"].iloc[-6:])],
        })
    rows.sort(key=lambda r: -(r["rel"]["12h"] or -999))
    risk = []
    for num, den, title, what, inv in RISK_RATIOS:
        if num not in adj.columns or (den and den not in adj.columns):
            risk.append({"title": title, "what": what, "value": None, "label": "doğrulanamadı"})
            continue
        s = (adj[num] / adj[den]).dropna() if den else adj[num].dropna()
        if len(s) < 30:
            risk.append({"title": title, "what": what, "value": None, "label": "doğrulanamadı"})
            continue
        c1w, c1m = _pct(s, 5), _pct(s, 21)
        sig = -c1m if inv else c1m
        lab = "RISK-ON" if sig is not None and sig > 2 else "RISK-OFF" if sig is not None and sig < -2 else "NÖTR"
        risk.append({"title": title, "what": what, "pair": f"{num}/{den}" if den else num, "value": float(s.iloc[-1]),
                     "c1w": c1w, "c1m": c1m, "band": band_pos(s), "label": lab, "inverted": inv})
    on = sum(r["label"] == "RISK-ON" for r in risk)
    off = sum(r["label"] == "RISK-OFF" for r in risk)
    mode = "RISK-ON" if on > off + 1 else "RISK-OFF" if off > on + 1 else "KARIŞIK"
    by_quad = {q: [r["symbol"] for r in rows if r["quad"] == q] for q in QUADS}
    sectors = [r for r in rows if r["group"] == "Sektör"]
    lead = [r for r in sectors if r["quad"] == "Lider"]
    lag = [r for r in sectors if r["quad"] == "Gecikmeli"]
    if rows:
        result = (f"Lider kadranda {len(by_quad['Lider'])}, gecikmeli kadranda {len(by_quad['Gecikmeli'])} fon; "
                  f"sektörlerde öne çıkan {', '.join(r['symbol'] for r in lead[:3]) or 'yok'}, geride kalan {', '.join(r['symbol'] for r in lag[:3]) or 'yok'}. "
                  f"Risk modu termometresi {mode.lower()} ({on} risk-on, {off} risk-off).")
    else:
        result = "Rotasyon hesaplanamadı: sektör fonu fiyatları doğrulanamadı."
    return {"day": day, "rows": rows, "by_quad": by_quad, "changes": [r for r in rows if r["changed"]],
            "risk": risk, "mode": mode, "on": on, "off": off, "result": result, "missing": missing,
            "mine": my_names(rows)}


def _sector_of(sym: str) -> str | None:
    f = stockdata.fmp_one("profile", 72, symbol=sym)
    sec = (f.data or {}).get("sector") if f else None
    if not sec:  # FMP rate limit / not covered → Yahoo
        y = stockdata.yahoo("info", sym, ttl_hours=72)
        sec = (y.data or {}).get("sector") if y else None
    return sec


def my_names(rows: list[dict]) -> list[dict]:
    quad = {r["symbol"]: r for r in rows}
    syms = {p["symbol"] for p in portfolio.positions() if p["asset"] == "stock"} | {t["symbol"] for t in db.active_tickers()}
    held = {p["symbol"] for p in portfolio.positions()}
    out = []
    for s in sorted(syms):
        try:
            sec = _sector_of(s)
        except Exception:  # noqa: BLE001
            sec = None
        etf = config.SECTOR_TO_ETF.get(sec or "")
        r = quad.get(etf) if etf else None
        out.append({"symbol": s, "sector": sec, "etf": etf, "quad": r["quad"] if r else None, "rel12": r["rel"]["12h"] if r else None,
                    "held": s in held})
    order = {"Lider": 0, "Toparlanan": 1, "Zayıflayan": 2, "Gecikmeli": 3, None: 4}
    return sorted(out, key=lambda x: order.get(x["quad"], 4))
