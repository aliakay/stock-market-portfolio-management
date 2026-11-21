"""Approximate options-positioning map from free Yahoo chains.

All of this is an estimate: Yahoo quotes are delayed, open interest updates
once a day, and dealer positioning is assumed (customers long calls/puts,
dealers short) — the usual simplification behind public "GEX" charts.
"""

from __future__ import annotations

import math
from datetime import datetime

from . import config, db, stockdata

R = 0.04  # risk-free used in Black-Scholes gamma


def _ncdf_pdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs_gamma(s: float, k: float, t: float, iv: float) -> float:
    if s <= 0 or k <= 0 or t <= 0 or not iv or iv <= 0:
        return 0.0
    d1 = (math.log(s / k) + (R + iv * iv / 2) * t) / (iv * math.sqrt(t))
    return _ncdf_pdf(d1) / (s * iv * math.sqrt(t))


def max_pain(calls: list[dict], puts: list[dict]) -> float | None:
    strikes = sorted({r["strike"] for r in calls + puts if r["strike"]})
    if not strikes:
        return None
    best, best_cost = None, None
    for k in strikes:
        cost = sum(max(0, k - c["strike"]) * c["oi"] for c in calls) + sum(max(0, p["strike"] - k) * p["oi"] for p in puts)
        if best_cost is None or cost < best_cost:
            best, best_cost = k, cost
    return best


def _mid(r: dict) -> float | None:
    if r.get("bid") and r.get("ask") and r["ask"] >= r["bid"] > 0:
        return (r["bid"] + r["ask"]) / 2
    return r.get("last")


def _years(expiry: str) -> float:
    now = datetime.now(config.NY_TZ)
    close = datetime.fromisoformat(expiry).replace(hour=16, tzinfo=config.NY_TZ)
    return max((close - now).total_seconds() / (365 * 86400), 1 / 365 / 6)


def _gex_at(spot: float, exps: list[dict]) -> float:
    total = 0.0
    for e in exps:
        t = _years(e["expiry"])
        for c in e["calls"]:
            total += bs_gamma(spot, c["strike"], t, c["iv"] or 0) * c["oi"] * 100 * spot * spot * 0.01
        for p in e["puts"]:
            total -= bs_gamma(spot, p["strike"], t, p["iv"] or 0) * p["oi"] * 100 * spot * spot * 0.01
    return total


def analyze(symbol: str) -> dict | None:
    f = stockdata.option_chains(symbol)
    if not f or not f.data.get("expiries"):
        return None
    d = f.data
    spot = d["spot"]
    exps = d["expiries"]
    if not spot:
        return None
    # clean: drop zero-OI noise, cap absurd IVs
    for e in exps:
        for side in ("calls", "puts"):
            e[side] = [r for r in e[side] if r["strike"] and (r["oi"] or r["vol"])]
            for r in e[side]:
                if r["iv"] is not None and (r["iv"] < 0.01 or r["iv"] > 5):
                    r["iv"] = None

    near = exps[0]
    calls_all = [c for e in exps for c in e["calls"]]
    puts_all = [p for e in exps for p in e["puts"]]

    def by_strike(rows):
        agg: dict[float, dict] = {}
        for r in rows:
            a = agg.setdefault(r["strike"], {"oi": 0, "vol": 0})
            a["oi"] += r["oi"]
            a["vol"] += r["vol"]
        return agg

    c_agg, p_agg = by_strike(calls_all), by_strike(puts_all)
    above = {k: v for k, v in c_agg.items() if k >= spot}
    below = {k: v for k, v in p_agg.items() if k <= spot}
    call_wall = max(above, key=lambda k: above[k]["oi"]) if above else None
    put_wall = max(below, key=lambda k: below[k]["oi"]) if below else None

    coi, poi = sum(v["oi"] for v in c_agg.values()), sum(v["oi"] for v in p_agg.values())
    cvol, pvol = sum(v["vol"] for v in c_agg.values()), sum(v["vol"] for v in p_agg.values())

    # ATM IV and expected move from nearest expiry ≥ 2 days out
    em_exp = next((e for e in exps if _years(e["expiry"]) * 365 >= 2), near)
    atm_iv = straddle = None
    if em_exp["calls"] and em_exp["puts"]:
        k_atm = min({c["strike"] for c in em_exp["calls"]} & {p["strike"] for p in em_exp["puts"]} or {spot},
                    key=lambda k: abs(k - spot))
        c = next((r for r in em_exp["calls"] if r["strike"] == k_atm), None)
        p = next((r for r in em_exp["puts"] if r["strike"] == k_atm), None)
        if c and p:
            ivs = [x for x in (c["iv"], p["iv"]) if x]
            atm_iv = sum(ivs) / len(ivs) if ivs else None
            cm, pm = _mid(c), _mid(p)
            straddle = (cm + pm) if cm and pm else None
    em_days = max(1, round(_years(em_exp["expiry"]) * 365))
    exp_move = straddle * 0.85 if straddle else (spot * atm_iv * math.sqrt(em_days / 365) if atm_iv else None)

    # Gamma: total now, and flip level by scanning hypothetical spots
    gex = _gex_at(spot, exps)
    flip = None
    grid = [spot * (0.85 + i * 0.005) for i in range(61)]
    vals = [(s, _gex_at(s, exps)) for s in grid]
    for (s1, g1), (s2, g2) in zip(vals, vals[1:]):
        if g1 == 0 or (g1 < 0) != (g2 < 0):
            flip = s1 + (s2 - s1) * (abs(g1) / (abs(g1) + abs(g2))) if g1 != g2 else s1
            if abs(flip - spot) / spot < 0.15:
                break

    # unusual: volume well above open interest on liquid contracts
    unusual = []
    for e in exps:
        for side, rows in (("CALL", e["calls"]), ("PUT", e["puts"])):
            for r in rows:
                if r["vol"] >= 500 and r["vol"] > 1.5 * max(r["oi"], 1):
                    unusual.append({"expiry": e["expiry"], "type": side, "strike": r["strike"], "vol": r["vol"], "oi": r["oi"],
                                    "ratio": r["vol"] / max(r["oi"], 1), "premium": (_mid(r) or 0) * r["vol"] * 100})
    unusual.sort(key=lambda u: u["premium"], reverse=True)

    # strike profile for the chart (±20% band)
    band = sorted(k for k in set(c_agg) | set(p_agg) if 0.8 * spot <= k <= 1.2 * spot)
    profile = [{"strike": k, "call_oi": c_agg.get(k, {}).get("oi", 0), "put_oi": p_agg.get(k, {}).get("oi", 0)} for k in band]

    today = datetime.now(config.NY_TZ).date().isoformat()
    if atm_iv:
        db.execute("INSERT OR REPLACE INTO iv_history(symbol, day, atm_iv) VALUES (?,?,?)", (symbol.upper(), today, atm_iv))
    hist = [r["atm_iv"] for r in db.query("SELECT atm_iv FROM iv_history WHERE symbol = ? ORDER BY day DESC LIMIT 252", (symbol.upper(),))]
    iv_rank = None
    if len(hist) >= 20 and atm_iv:
        lo, hi = min(hist), max(hist)
        iv_rank = (atm_iv - lo) / (hi - lo) * 100 if hi > lo else None

    return {
        "symbol": symbol.upper(), "spot": spot, "fetched_at": f.fetched_at, "stamp": stockdata.stamp(f),
        "expiries": [e["expiry"] for e in exps], "near_expiry": near["expiry"],
        "call_wall": call_wall, "put_wall": put_wall,
        "max_pain": max_pain(near["calls"], near["puts"]), "max_pain_expiry": near["expiry"],
        "pc_oi": poi / coi if coi else None, "pc_vol": pvol / cvol if cvol else None,
        "call_oi": coi, "put_oi": poi, "call_vol": cvol, "put_vol": pvol,
        "atm_iv": atm_iv, "iv_rank": iv_rank, "iv_hist_n": len(hist),
        "exp_move": exp_move, "exp_move_pct": exp_move / spot * 100 if exp_move else None,
        "exp_move_expiry": em_exp["expiry"], "exp_move_days": em_days,
        "gex": gex, "gamma_sign": "pozitif" if gex > 0 else "negatif", "gamma_flip": flip,
        "unusual": unusual[:8], "profile": profile,
    }


def score(o: dict | None, hv20: float | None) -> tuple[float | None, list[str]]:
    """0-100 options/flow tone with reasons. Bullish = call-heavy flow, positive gamma, price above put wall."""
    if not o:
        return None, []
    pts, why = 50.0, []
    if o["pc_oi"] is not None:
        adj = max(-15, min(15, (1 - o["pc_oi"]) * 20))
        pts += adj
        why.append(f"Put/Call OI {o['pc_oi']:.2f}".replace(".", ","))
    if o["pc_vol"] is not None:
        adj = max(-10, min(10, (1 - o["pc_vol"]) * 15))
        pts += adj
        why.append(f"Put/Call hacim {o['pc_vol']:.2f}".replace(".", ","))
    pts += 8 if o["gex"] > 0 else -8
    why.append(f"Gamma {o['gamma_sign']}")
    if o["put_wall"] and o["call_wall"]:
        rng = o["call_wall"] - o["put_wall"]
        if rng > 0:
            pos = (o["spot"] - o["put_wall"]) / rng
            pts += 5 if 0.2 <= pos <= 0.8 else -3
    calls_u = sum(u["premium"] for u in o["unusual"] if u["type"] == "CALL")
    puts_u = sum(u["premium"] for u in o["unusual"] if u["type"] == "PUT")
    if calls_u + puts_u > 0:
        tilt = (calls_u - puts_u) / (calls_u + puts_u)
        pts += tilt * 8
        why.append("olağandışı akış " + ("call ağırlıklı" if tilt > 0 else "put ağırlıklı"))
    if o["atm_iv"] and hv20:
        prem = o["atm_iv"] / hv20
        if prem > 1.5:
            pts -= 5
            why.append(f"IV/HV {prem:.1f} (korku primi)".replace(".", ","))
    return round(max(0, min(100, pts)), 1), why
