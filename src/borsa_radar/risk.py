"""Portfolio risk: concentration, correlation, beta, volatility, VaR, drawdown, stress scenarios.

Everything is computed from the shared daily price store and the portfolio ledger; no model, no paid data.
Ideas borrowed (re-implemented) from open-source trading toolkits: correlation matrix and clusters, factor
shocks, historical replay of crisis windows with today's weights, and a regime-based exposure ceiling.
"""

from __future__ import annotations

import math

import numpy as np

from . import config, portfolio, prices, stockdata
from .util import tr_n

WIN_CORR = 126      # ~6 months of daily returns for correlation / covariance / volatility
WIN_BETA = 252      # ~1 year for betas and historical VaR
CORR_EDGE = 0.55    # pairs above this move together → same cluster

HISTORICAL = [  # (name, start, end) — replayed with today's holdings
    ("Ağustos 2024 · yen carry çözülmesi", "2024-07-10", "2024-08-05"),
    ("Şubat–Nisan 2025 · gümrük tarifesi şoku", "2025-02-19", "2025-04-08"),
]
FACTOR_SHOCKS = [  # (name, factor symbol, shock)
    ("S&P 500 %10 düşerse", "SPY", -0.10),
    ("Nasdaq 100 %15 düşerse", "QQQ", -0.15),
    ("Yarı iletkenler (SMH) %20 düşerse", "SMH", -0.20),
    ("Bitcoin %30 düşerse", "BTC-USD", -0.30),
]
# regime score → suggested maximum stock exposure (rule, not measured; see /piyasa sicil for the regime itself)
CEILING = [(70, 100), (55, 80), (40, 60), (25, 40), (0, 25)]
LIMITS = {"single": 20.0, "top3": 50.0, "cluster": 40.0, "sector": 40.0, "beta": 1.5, "var": 3.0, "corr": 0.5, "dd": 20.0}


def _returns(s: prices.Series, days: list[str]) -> list[float | None]:
    """Daily close-to-close returns on the given trading-day calendar (None where data is missing)."""
    out: list[float | None] = []
    prev = None
    first = s.days[0] if s else None
    for d in days:
        px = s.at(d) if s and first and d >= first else None
        out.append(px / prev - 1 if px is not None and prev else None)
        prev = px
    return out


def _beta(r: list, f: list) -> tuple[float | None, int]:
    pairs = [(a, b) for a, b in zip(r, f) if a is not None and b is not None]
    if len(pairs) < 40:
        return None, len(pairs)
    x = np.array([b for _, b in pairs])
    y = np.array([a for a, _ in pairs])
    v = x.var()
    return (float(np.cov(y, x, bias=True)[0, 1] / v) if v > 0 else None), len(pairs)


def _window_ret(s: prices.Series, d0: str, d1: str) -> float | None:
    if not s or s.days[0] > d0:
        return None
    a, b = s.at(d0), s.at(d1)
    return b / a - 1 if a and b else None


_SECTORS: dict[str, tuple[str, str]] = {}
_CACHE: dict = {}


def _sector(sym: str) -> tuple[str, str]:
    """Sector / industry from the cached FMP profile (30-day TTL, so normally no API call)."""
    if sym in _SECTORS:
        return _SECTORS[sym]
    p: dict = {}
    try:
        p = stockdata.fmp_one("profile", ttl_hours=24 * 30, symbol=sym).data or {}
    except Exception:  # noqa: BLE001
        pass
    if not p.get("sector"):
        try:  # Yahoo fallback (free plan 402/429 on FMP)
            p = stockdata.yahoo("info", sym, ttl_hours=24 * 30).data or {}
        except Exception:  # noqa: BLE001
            p = {}
    out = (p.get("sector") or "Bilinmiyor", p.get("industry") or "—")
    if out[0] != "Bilinmiyor":
        _SECTORS[sym] = out
    return out


def ceiling(score: float | None) -> int | None:
    if score is None:
        return None
    return next(c for lo, c in CEILING if score >= lo)


def compute(led: portfolio.Ledger | None = None, max_age: float = 600) -> dict | None:
    import time
    led = led or portfolio.build()
    hit = _CACHE.get("r")
    if hit and hit[0] == (led.days[-1] if led.days else None, len(led.total), round(led.total[-1]["value"], 2) if led.total else 0) \
            and time.time() - hit[1] < max_age:
        return hit[2]
    out = _compute(led)
    _CACHE["r"] = ((led.days[-1] if led.days else None, len(led.total), round(led.total[-1]["value"], 2) if led.total else 0),
                   time.time(), out)
    return out


def _compute(led: portfolio.Ledger, sector=None) -> dict | None:
    """sector: optional symbol → (sector, industry) lookup; default `_sector` (may call FMP/Yahoo for uncached profiles)."""
    sector = sector or _sector
    if not led.days:
        return None
    pos = [p for p in portfolio.positions(led=led) if p["value"] > 1]
    total = led.total[-1]["value"]
    cash = led.total[-1]["cash"]
    as_of = led.days[-1]
    if not pos or total <= 0:
        return {"as_of": as_of, "empty": True}
    syms = [p["symbol"] for p in pos]
    w = np.array([p["value"] / total for p in pos])          # fraction of total portfolio (cash has weight, beta 0)
    factors = {f for _, f, _ in FACTOR_SHOCKS}
    ser = prices.series_many(syms + list(factors), start=None)
    cal = prices.series(config.CALENDAR_SYMBOL).days
    cal = [d for d in cal if d <= as_of]
    days_b = cal[-(WIN_BETA + 1):]
    rets_b = {s: _returns(ser[s], days_b)[1:] for s in syms + list(factors)}
    days_b = days_b[1:]

    # ── per holding: beta to each factor, volatility ──
    rows = []
    for p, wi in zip(pos, w):
        r = rets_b[p["symbol"]]
        betas = {f: _beta(r, rets_b[f])[0] for f in factors}
        rc = [x for x in r[-WIN_CORR:] if x is not None]
        vol = float(np.std(rc) * math.sqrt(252) * 100) if len(rc) >= 40 else None
        n_obs = sum(x is not None for x in r)
        sec, ind = sector(p["symbol"])
        rows.append({"symbol": p["symbol"], "value": p["value"], "weight": float(wi) * 100, "unreal_pct": p["unreal_pct"],
                     "beta": betas["SPY"], "beta_smh": betas["SMH"], "beta_btc": betas["BTC-USD"], "beta_qqq": betas["QQQ"],
                     "vol": vol, "obs": n_obs, "short_history": n_obs < WIN_BETA * 0.6, "sector": sec, "industry": ind})

    # ── correlation / covariance on the common window ──
    win = list(range(max(0, len(days_b) - WIN_CORR), len(days_b)))
    usable = [i for i, s in enumerate(syms) if sum(rets_b[s][k] is not None for k in win) >= 0.8 * len(win)]
    corr = None
    contrib = {}
    ex_ante_vol = None
    avg_corr = None
    if len(usable) >= 2:
        keep = [k for k in win if all(rets_b[syms[i]][k] is not None for i in usable)]
        m = np.array([[rets_b[syms[i]][k] for k in keep] for i in usable])
        c = np.corrcoef(m)
        cov = np.cov(m)
        wu = w[usable]
        pv = float(wu @ cov @ wu)
        ex_ante_vol = math.sqrt(pv * 252) * 100 if pv > 0 else None
        if pv > 0:
            mc = cov @ wu
            for j, i in enumerate(usable):
                contrib[syms[i]] = float(wu[j] * mc[j] / pv * 100)
        corr = {"symbols": [syms[i] for i in usable], "matrix": [[round(float(x), 2) for x in row] for row in c], "days": len(keep)}
        iu = np.triu_indices(len(usable), 1)
        ww = np.outer(wu, wu)[iu]
        avg_corr = float((c[iu] * ww).sum() / ww.sum()) if ww.sum() > 0 else None
    for r in rows:
        r["risk_share"] = contrib.get(r["symbol"])

    # ── clusters: holdings that move together (correlation ≥ CORR_EDGE) ──
    clusters = []
    if corr:
        cs = corr["symbols"]
        parent = list(range(len(cs)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        for a in range(len(cs)):
            for b in range(a + 1, len(cs)):
                if corr["matrix"][a][b] >= CORR_EDGE:
                    parent[find(a)] = find(b)
        groups: dict[int, list[str]] = {}
        for a in range(len(cs)):
            groups.setdefault(find(a), []).append(cs[a])
        wmap = {r["symbol"]: r["weight"] for r in rows}
        for g in groups.values():
            if len(g) >= 2:
                pairs = [corr["matrix"][cs.index(x)][cs.index(y)] for i, x in enumerate(g) for y in g[i + 1:]]
                clusters.append({"symbols": sorted(g, key=lambda s: -wmap[s]), "weight": sum(wmap[s] for s in g),
                                 "avg_corr": sum(pairs) / len(pairs)})
        clusters.sort(key=lambda g: -g["weight"])

    # ── portfolio beta, historical VaR with today's weights ──
    wmap = dict(zip(syms, w))
    beta_p = sum(wmap[r["symbol"]] * r["beta"] for r in rows if r["beta"] is not None)
    proxied = [r["symbol"] for r in rows if r["beta"] is None]
    port_r = []
    for k in range(len(days_b)):
        tot, ok = 0.0, True
        for r in rows:
            x = rets_b[r["symbol"]][k]
            if x is None:  # missing history (recent IPO): stand in with beta × S&P
                f = rets_b["SPY"][k]
                if f is None:
                    ok = False
                    break
                x = (r["beta"] if r["beta"] is not None else 1.0) * f
            tot += wmap[r["symbol"]] * x
        if ok:
            port_r.append(tot)
    var = cvar = None
    if len(port_r) >= 60:
        arr = np.array(port_r)
        q = float(np.percentile(arr, 5))
        var = -q * 100
        cvar = -float(arr[arr <= q].mean()) * 100

    # ── realized drawdown from the portfolio's own time-weighted index ──
    peak, peak_day, max_dd, max_dd_day = 0.0, None, 0.0, None
    for t in led.total:
        if t["idx"] > peak:
            peak, peak_day = t["idx"], t["day"]
        dd = (t["idx"] / peak - 1) * 100 if peak else 0
        if dd < max_dd:
            max_dd, max_dd_day = dd, t["day"]
    cur_dd = (led.total[-1]["idx"] / peak - 1) * 100 if peak else None
    realized_vol = None
    idx = [t["idx"] for t in led.total[-(WIN_CORR + 1):]]
    if len(idx) > 40:
        rr = np.diff(idx) / np.array(idx[:-1])
        realized_vol = float(rr.std() * math.sqrt(252) * 100)

    # ── stress tests ──
    stress = []
    for name, f, shock in FACTOR_SHOCKS:
        key = {"SPY": "beta", "QQQ": "beta_qqq", "SMH": "beta_smh", "BTC-USD": "beta_btc"}[f]
        per = [(r["symbol"], r["value"] * (r[key] or 0) * shock) for r in rows]
        loss = sum(x for _, x in per)
        worst = sorted(per, key=lambda t: t[1])[:3]
        stress.append({"name": name, "kind": "faktör", "pnl": loss, "pct": loss / total * 100,
                       "worst": [{"symbol": s, "pnl": x} for s, x in worst if x < 0]})
    spy = ser["SPY"]
    hist = list(HISTORICAL)
    if spy and len(spy.days) > 30:  # SPY's worst 21-day stretch in the stored history
        sd, sc = spy.days, spy.closes
        k = min(range(21, len(sc)), key=lambda i: sc[i] / sc[i - 21])
        hist.append((f"S&P'nin bu veride en kötü ayı ({sd[k - 21]} → {sd[k]})", sd[k - 21], sd[k]))
    for name, d0, d1 in hist:
        f_ret = _window_ret(spy, d0, d1)
        per, approx = [], []
        for r in rows:
            x = _window_ret(ser[r["symbol"]], d0, d1)
            if x is None:
                if f_ret is None:
                    continue
                x = (r["beta"] if r["beta"] is not None else 1.0) * f_ret
                approx.append(r["symbol"])
            per.append((r["symbol"], r["value"] * x))
        loss = sum(x for _, x in per)
        stress.append({"name": name, "kind": "geçmiş", "d0": d0, "d1": d1, "spy_pct": f_ret * 100 if f_ret is not None else None,
                       "pnl": loss, "pct": loss / total * 100, "approx": approx,
                       "worst": [{"symbol": s, "pnl": x} for s, x in sorted(per, key=lambda t: t[1])[:3] if x < 0]})

    # ── concentration and sectors ──
    ws = sorted((r["weight"] for r in rows), reverse=True)
    hhi = sum((x / 100) ** 2 for x in ws)
    sectors: dict[str, float] = {}
    for r in rows:
        sectors[r["sector"]] = sectors.get(r["sector"], 0) + r["weight"]
    industries: dict[str, float] = {}
    for r in rows:
        industries[r["industry"]] = industries.get(r["industry"], 0) + r["weight"]
    stock_pct = sum(ws)

    from . import regime
    reg = regime.latest()
    ceil = ceiling(reg["overall"]) if reg else None

    out = {"as_of": as_of, "total": total, "cash": cash, "cash_pct": cash / total * 100, "stock_pct": stock_pct,
           "n": len(rows), "top1": ws[0], "top3": sum(ws[:3]), "effective_n": 1 / hhi if hhi else None,
           "beta": beta_p, "beta_proxied": proxied, "vol_ex_ante": ex_ante_vol, "vol_realized": realized_vol,
           "var95": var, "cvar95": cvar, "var95_usd": var * total / 100 if var is not None else None,
           "avg_corr": avg_corr, "max_dd": max_dd, "max_dd_day": max_dd_day, "cur_dd": cur_dd, "peak_day": peak_day,
           "holdings": rows, "corr": corr, "clusters": clusters, "stress": stress,
           "sectors": sorted(((k, float(v)) for k, v in sectors.items()), key=lambda t: -t[1]),
           "industries": sorted(((k, float(v)) for k, v in industries.items()), key=lambda t: -t[1]),
           "regime": {"day": reg["day"], "label": reg["label"], "score": reg["overall"]} if reg else None,
           "ceiling": ceil, "windows": {"corr_days": WIN_CORR, "beta_days": WIN_BETA},
           "sources": ["radar defteri", "Yahoo günlük kapanış", "FMP şirket profili (sektör)", "radar rejim"]}
    out["warnings"] = warnings(out)
    out["summary"] = summary(out)
    return out


def warnings(r: dict) -> list[dict]:
    w = []
    h = sorted(r["holdings"], key=lambda x: -x["weight"])
    if h and h[0]["weight"] > LIMITS["single"]:
        w.append({"key": "single", "level": "warn", "text": f"Tek hisse ağırlığı yüksek: {h[0]['symbol']} %{tr_n(h[0]['weight'], 1)} (eşik %{tr_n(LIMITS['single'], 0)})."})
    if r["top3"] > LIMITS["top3"]:
        w.append({"key": "top3", "level": "info", "text": f"İlk 3 pozisyon portföyün %{tr_n(r['top3'], 1)}'i (eşik %{tr_n(LIMITS['top3'], 0)})."})
    for c in r["clusters"]:
        if c["weight"] > LIMITS["cluster"]:
            w.append({"key": "cluster", "level": "warn",
                      "text": f"Birlikte hareket eden küme %{tr_n(c['weight'], 1)}: {', '.join(c['symbols'])} (ort. korelasyon {tr_n(c['avg_corr'], 2)}). "
                              "Bunlar aynı gün birlikte düşer; çeşitlendirme sanıldığından az."})
    for name, x in r["sectors"]:
        if x > LIMITS["sector"] and name != "Bilinmiyor":
            w.append({"key": "sector", "level": "warn", "text": f"Sektör yoğunluğu: {name} %{tr_n(x, 1)} (eşik %{tr_n(LIMITS['sector'], 0)})."})
    if r["beta"] and r["beta"] > LIMITS["beta"]:
        w.append({"key": "beta", "level": "warn", "text": f"Portföy betası {tr_n(r['beta'], 2)}: S&P %1 oynadığında portföy ortalama %{tr_n(r['beta'], 1)} oynar."})
    if r["var95"] and r["var95"] > LIMITS["var"]:
        w.append({"key": "var", "level": "warn", "text": f"Günlük riske maruz değer (%95) %{tr_n(r['var95'], 1)} ≈ {tr_n(r['var95_usd'], 0)}$: 20 işlem gününün 1'inde bundan kötü gün beklenir."})
    if r["avg_corr"] and r["avg_corr"] > LIMITS["corr"]:
        w.append({"key": "corr", "level": "info", "text": f"Pozisyonlar arası ağırlıklı ortalama korelasyon {tr_n(r['avg_corr'], 2)} (eşik {LIMITS['corr']})."})
    if r["cur_dd"] is not None and r["cur_dd"] < -LIMITS["dd"]:
        w.append({"key": "dd", "level": "warn", "text": f"Portföy zirvesinden %{tr_n(-r['cur_dd'], 1)} aşağıda (zirve {r['peak_day']})."})
    if r["ceiling"] is not None and r["stock_pct"] > r["ceiling"] + 5:
        w.append({"key": "ceiling", "level": "info",
                  "text": f"Hisse oranı %{tr_n(r['stock_pct'], 0)}; {r['regime']['label']} rejiminde kural tavanı %{r['ceiling']} (yeni alımlar için ölçü, satış önerisi değil)."})
    for x in h:
        if x["unreal_pct"] is not None and x["unreal_pct"] < -25:
            w.append({"key": "loser", "level": "info", "symbol": x["symbol"],
                      "text": f"{x['symbol']} maliyetin %{tr_n(-x['unreal_pct'], 0)} altında: tezin hâlâ geçerli mi? (Tez defterine yaz.)"})
    return w


def summary(r: dict) -> str:
    parts = [f"{r['as_of']} kapanışına göre {r['n']} pozisyon, hisse oranı %{tr_n(r['stock_pct'], 0)}."]
    if r["beta"]:
        parts.append(f"Portföy betası {tr_n(r['beta'], 2)}.")
    if r["clusters"]:
        c = r["clusters"][0]
        parts.append(f"En büyük birlikte-hareket kümesi %{tr_n(c['weight'], 0)} ({', '.join(c['symbols'][:4])}{'…' if len(c['symbols']) > 4 else ''}).")
    s = next((x for x in r["stress"] if x["name"].startswith("S&P 500")), None)
    if s:
        parts.append(f"S&P %10 düşüşünde tahmini etki {tr_n(s['pnl'], 0)}$ (%{tr_n(s['pct'], 1)}).")
    return " ".join(parts)


def brief(r: dict | None) -> dict | None:
    """Compact version for context snapshots / Hermes."""
    if not r or r.get("empty"):
        return None
    rd = lambda v, d=2: None if v is None else round(v, d)  # noqa: E731
    return {"as_of": r["as_of"], "beta": rd(r["beta"]), "vol_ex_ante_pct": rd(r["vol_ex_ante"], 1), "var95_1d_pct": rd(r["var95"]),
            "var95_1d_usd": rd(r["var95_usd"], 0), "top1_pct": rd(r["top1"], 1), "top3_pct": rd(r["top3"], 1),
            "effective_n": rd(r["effective_n"], 1), "avg_corr": rd(r["avg_corr"]), "drawdown_from_peak_pct": rd(r["cur_dd"], 1),
            "max_drawdown_pct": rd(r["max_dd"], 1), "stock_pct": rd(r["stock_pct"], 1), "regime_ceiling_pct": r["ceiling"],
            "clusters": [{"symbols": c["symbols"], "weight_pct": rd(c["weight"], 1)} for c in r["clusters"]],
            "sectors": [{"sector": s, "weight_pct": rd(x, 1)} for s, x in r["sectors"]],
            "stress": [{"name": s["name"], "pnl_usd": rd(s["pnl"], 0), "pct": rd(s["pct"], 1)} for s in r["stress"]],
            "holdings": [{"symbol": h["symbol"], "weight_pct": rd(h["weight"], 1), "beta": rd(h["beta"]), "vol_pct": rd(h["vol"], 0),
                          "risk_share_pct": rd(h["risk_share"], 1), "sector": h["sector"]} for h in r["holdings"]],
            "warnings": [w["text"] for w in r["warnings"]], "summary": r["summary"]}
