"""Trade behavior analytics ("Davranış"): process coaching from the closed round trips, never buy/sell advice.

Inputs are the ledger's closed round trips (portfolio.closed_positions), open positions, the daily portfolio
value series and stored daily closes. Every finding carries its sample size and the numbers behind it; small
samples are labelled. Idea (re-implemented): process-adherence / behavior-pattern review with reflection
questions, as in open-source "trade performance coach" tooling.
"""

from __future__ import annotations

import statistics
import time
from bisect import bisect_right
from datetime import date, timedelta

from .util import tr_n

SMALL_N = 20              # below this a finding is labelled "küçük örneklem"
BUCKETS = [("<7 gün", 0, 7), ("7–30 gün", 7, 30), ("30–90 gün", 30, 90), ("90+ gün", 90, None)]
POST_LOSS_DAYS = 3        # trips opened within N calendar days after a losing trip closed
SIZE_FLAG = 20.0          # % of portfolio value
DRIFT = (20, 60)          # trading days after exit
BUSY_MIN = 4              # a month needs at least this many closed trips to count as "busy"

_CACHE: dict = {}


# ── small helpers ───────────────────────────────────────────

def _d(s: str) -> date:
    return date.fromisoformat(s)


def _med(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _win(t: dict) -> bool:
    return t["realized"] > 0


def _worse(a: float | None, b: float | None) -> bool:
    """`a` meaningfully below `b` (more than $10 and 25% of |b|), so tiny gaps don't raise a flag."""
    return a is not None and b is not None and a < b - max(10.0, 0.25 * abs(b))


def _note(n: int) -> str | None:
    return f"küçük örneklem (n={n})" if n < SMALL_N else None


def _usd(v: float | None, sign: bool = True) -> str:
    return "—" if v is None else f"{tr_n(v, 0, sign=sign)}$"


def _p(v: float | None, digits: int = 1, sign: bool = True) -> str:
    return "—" if v is None else f"%{tr_n(v, digits, sign=sign)}"


# ── pure computations on trip lists ─────────────────────────

def stats(trips: list[dict]) -> dict:
    """Win rate, average win/loss, payoff, profit factor, expectancy for a list of round trips."""
    n = len(trips)
    wins = [t for t in trips if _win(t)]
    losses = [t for t in trips if not _win(t)]
    gw = sum(t["realized"] for t in wins)
    gl = sum(t["realized"] for t in losses)
    avg_w = gw / len(wins) if wins else None
    avg_l = gl / len(losses) if losses else None
    cost = sum(t["cost"] for t in trips)
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "hit": len(wins) / n * 100 if n else None,
        "avg_win": avg_w, "avg_loss": avg_l,
        "avg_win_pct": _mean([t.get("pct") for t in wins]), "avg_loss_pct": _mean([t.get("pct") for t in losses]),
        "payoff": avg_w / abs(avg_l) if avg_w is not None and avg_l else None,
        "profit_factor": gw / abs(gl) if gl else None,
        "gross_win": gw, "gross_loss": gl,
        "exp": sum(t["realized"] for t in trips) / n if n else None,
        "exp_pct": _mean([t.get("pct") for t in trips]),
        "sum": sum(t["realized"] for t in trips),
        "cost_weighted_pct": sum(t["realized"] for t in trips) / cost * 100 if cost > 0 else None,
        "median_days": _med([t["days"] for t in trips]),
        "note": _note(n),
    }


def holding_buckets(trips: list[dict]) -> list[dict]:
    out = []
    for label, lo, hi in BUCKETS:
        sel = [t for t in trips if t["days"] >= lo and (hi is None or t["days"] < hi)]
        s = stats(sel)
        out.append({"label": label, "lo": lo, "hi": hi, "n": s["n"], "hit": s["hit"], "exp": s["exp"], "exp_pct": s["exp_pct"],
                    "sum": s["sum"], "note": s["note"]})
    return out


def disposition(trips: list[dict], open_pos: list[dict], as_of: str | None) -> dict:
    """Do losers get held longer than winners? Which open losing positions are already older than a typical loser?"""
    w = [t["days"] for t in trips if _win(t)]
    lo = [t["days"] for t in trips if not _win(t)]
    mw, ml = _med(w), _med(lo)
    held = []
    if ml is not None and as_of:
        for p in open_pos:
            if p.get("unreal_pct") is None or p["unreal_pct"] >= 0 or not p.get("first_day"):
                continue
            days = (_d(as_of) - _d(p["first_day"])).days
            if days > ml:
                held.append({"symbol": p["symbol"], "days": days, "unreal_pct": p["unreal_pct"], "unreal": p.get("unreal"),
                             "weight": p.get("weight"), "first_day": p["first_day"]})
    held.sort(key=lambda x: -x["days"])
    return {"n_win": len(w), "n_loss": len(lo), "median_win_days": mw, "median_loss_days": ml,
            "ratio": ml / mw if mw and ml is not None else None, "open_held_long": held, "note": _note(min(len(w), len(lo)))}


def post_loss(trips: list[dict], sizes: dict | None = None, window: int = POST_LOSS_DAYS) -> dict:
    """Trips opened within `window` calendar days after a losing trip closed vs all other trips."""
    losers = [t for t in trips if not _win(t)]
    after, same_sym = [], 0
    for t in trips:
        o = _d(t["open"])
        trig = [lt for lt in losers if lt is not t and 0 <= (o - _d(lt["close"])).days <= window]
        if trig:
            after.append(t)
            if any(lt["symbol"] == t["symbol"] for lt in trig):
                same_sym += 1
    ids = {id(t) for t in after}
    base = [t for t in trips if id(t) not in ids]
    sa, sb = stats(after), stats(base)
    med_cost_all = _med([t["cost"] for t in trips])
    med_cost_after = _med([t["cost"] for t in after])
    sz = sizes or {}
    med_sz_after = _med([sz.get(id(t)) for t in after])
    med_sz_base = _med([sz.get(id(t)) for t in base])
    return {"window": window, "after": sa, "base": sb, "same_symbol": same_sym,
            "median_cost_after": med_cost_after, "median_cost_all": med_cost_all,
            "cost_ratio": med_cost_after / med_cost_all if med_cost_after and med_cost_all else None,
            "median_size_after": med_sz_after, "median_size_base": med_sz_base,
            "trips": sorted(({"symbol": t["symbol"], "open": t["open"], "close": t["close"], "realized": t["realized"],
                              "pct": t.get("pct"), "cost": t["cost"]} for t in after), key=lambda x: x["open"], reverse=True),
            "note": _note(sa["n"])}


def split_by(trips: list[dict], key: str) -> dict:
    """Trips with trip[key] > 0 vs == 0 (avg_down / add_up)."""
    yes = [t for t in trips if (t.get(key) or 0) > 0]
    no = [t for t in trips if not (t.get(key) or 0) > 0]
    return {"key": key, "yes": stats(yes), "no": stats(no), "events": sum(t.get(key) or 0 for t in trips),
            "symbols": sorted({t["symbol"] for t in yes}), "note": _note(len(yes))}


def after_exit(trips: list[dict], series: dict, horizons: tuple[int, ...] = DRIFT) -> dict:
    """Close N trading days after the exit vs the exit price, separately for winning and losing trips."""
    rows = []
    skipped = {h: 0 for h in horizons}
    for t in trips:
        s = series.get(t["symbol"])
        px = t.get("exit_px")
        if not s or not px:
            for h in horizons:
                skipped[h] += 1
            continue
        i = bisect_right(s.days, t["close"]) - 1
        ref = s.closes[i] if i >= 0 else None
        if ref is None or not (0.5 < ref / px < 2):  # price basis mismatch (split / currency) → don't guess
            for h in horizons:
                skipped[h] += 1
            continue
        row = {"symbol": t["symbol"], "close": t["close"], "exit_px": px, "win": _win(t), "pct": t.get("pct"),
               "realized": t["realized"]}
        for h in horizons:
            if i + h < len(s.days):
                row[f"d{h}"] = (s.closes[i + h] / px - 1) * 100
                row[f"day{h}"] = s.days[i + h]
            else:
                row[f"d{h}"] = None
                skipped[h] += 1
        rows.append(row)
    groups = {}
    for name, flag in (("win", True), ("loss", False)):
        g = {}
        for h in horizons:
            xs = [r[f"d{h}"] for r in rows if r["win"] is flag and r[f"d{h}"] is not None]
            g[h] = {"n": len(xs), "median": _med(xs), "mean": _mean(xs),
                    "up_share": sum(x > 0 for x in xs) / len(xs) * 100 if xs else None,
                    "big_up_share": sum(x > 10 for x in xs) / len(xs) * 100 if xs else None, "note": _note(len(xs))}
        groups[name] = g
    h0 = horizons[0]
    early = sorted((r for r in rows if r["win"] and r[f"d{h0}"] is not None), key=lambda r: -r[f"d{h0}"])[:5]
    recov = sorted((r for r in rows if not r["win"] and r[f"d{h0}"] is not None), key=lambda r: -r[f"d{h0}"])[:5]
    return {"horizons": list(horizons), "groups": groups, "skipped": skipped, "early_exits": early, "recovered": recov,
            "rows": rows}


def _value_lookup(total: list[dict]):
    days = [t["day"] for t in total]
    vals = [t["value"] for t in total]

    def at(day: str) -> float | None:
        i = bisect_right(days, day) - 1
        return vals[i] if i >= 0 else None

    def max_between(d0: str, d1: str) -> float | None:
        i0 = max(0, bisect_right(days, d0) - 1)
        i1 = bisect_right(days, d1)
        seg = vals[i0:i1]
        return max(seg) if seg else None
    return at, max_between


def sizing(trips: list[dict], total: list[dict], flag: float = SIZE_FLAG) -> dict:
    """Position size as % of portfolio value. Single-buy trips: cost / value on the open day (exact).
    Scaled-in trips: peak cost basis / highest portfolio value during the trip (a lower bound)."""
    at, max_between = _value_lookup(total)
    rows, sizes = [], {}
    for t in trips:
        if t.get("buys", 1) <= 1:
            v = at(t["open"])
            pct = t["cost"] / v * 100 if v and v > 0 else None
            basis = "tek alım"
        else:
            v = max_between(t["open"], t["close"])
            pct = (t.get("max_cost") or t["cost"]) / v * 100 if v and v > 0 else None
            basis = "kademeli (alt sınır)"
        if pct is None:
            continue
        sizes[id(t)] = pct
        rows.append({"symbol": t["symbol"], "open": t["open"], "close": t["close"], "size_pct": pct, "basis": basis,
                     "portfolio_value": v, "realized": t["realized"], "pct": t.get("pct"), "buys": t.get("buys")})
    big = [t for t in trips if sizes.get(id(t), 0) > flag]
    rest = [t for t in trips if id(t) in sizes and sizes[id(t)] <= flag]
    by_year: dict[str, list[float]] = {}
    for r in rows:
        by_year.setdefault(r["open"][:4], []).append(r["size_pct"])
    return {"flag": flag, "n": len(rows), "median": _med([r["size_pct"] for r in rows]),
            "big": stats(big), "rest": stats(rest),
            "flagged": sorted((r for r in rows if r["size_pct"] > flag), key=lambda r: r["open"], reverse=True),
            "by_year": [{"year": y, "n": len(v), "median": _med(v), "over": sum(x > flag for x in v)} for y, v in sorted(by_year.items())],
            "_sizes": sizes}


def monthly(trips: list[dict]) -> dict:
    m: dict[str, list[dict]] = {}
    for t in trips:
        m.setdefault(t["close"][:7], []).append(t)
    rows = [{"month": k, "n": len(v), "realized": sum(t["realized"] for t in v), "wins": sum(_win(t) for t in v),
             "hit": sum(_win(t) for t in v) / len(v) * 100} for k, v in sorted(m.items())]
    med_n = _med([r["n"] for r in rows])
    thr = max(BUSY_MIN, 2 * med_n) if med_n is not None else None
    busy_months = {r["month"] for r in rows if thr is not None and r["n"] >= thr}
    for r in rows:
        r["busy"] = r["month"] in busy_months
    busy = [t for t in trips if t["close"][:7] in busy_months]
    calm = [t for t in trips if t["close"][:7] not in busy_months]
    return {"rows": rows, "median_n": med_n, "threshold": thr, "busy": stats(busy), "calm": stats(calm),
            "busy_months": sorted(busy_months), "note": _note(len(busy))}


def by_symbol(trips: list[dict], k: int = 5) -> dict:
    m: dict[str, list[dict]] = {}
    for t in trips:
        m.setdefault(t["symbol"], []).append(t)
    rows = [{"symbol": s, "n": len(v), "realized": sum(t["realized"] for t in v), "wins": sum(_win(t) for t in v)} for s, v in m.items()]
    rows.sort(key=lambda r: -r["realized"])
    top = [r for r in rows if r["realized"] > 0][:k]
    bottom = sorted((r for r in rows if r["realized"] < 0), key=lambda r: r["realized"])[:k]
    return {"top": top, "bottom": bottom, "n_symbols": len(rows), "repeat": sorted((r for r in rows if r["n"] >= 3), key=lambda r: -r["n"])}


# ── findings and summary (template text, no LLM) ────────────

def findings(r: dict) -> list[dict]:
    out: list[dict] = []

    def add(key, tone, title, text, n, evidence, question):
        out.append({"key": key, "tone": tone, "title": title, "text": text, "n": n, "note": _note(n),
                    "evidence": evidence, "question": question})

    d = r["disposition"]
    if d["median_win_days"] is not None and d["median_loss_days"] is not None:
        mw, ml = d["median_win_days"], d["median_loss_days"]
        longer = ml > mw * 1.2
        txt = (f"Kaybeden işlemler medyan {tr_n(ml, 0)} gün, kazananlar {tr_n(mw, 0)} gün tutulmuş."
               + (" Kaybedenler daha uzun tutuluyor (elden çıkarma etkisi örüntüsü)." if longer else " Kaybedenleri kazananlardan uzun tutma örüntüsü görünmüyor."))
        if d["open_held_long"]:
            txt += f" Şu an zararda olup tipik kaybeden süresini aşan açık pozisyon: {', '.join(x['symbol'] for x in d['open_held_long'])}."
        add("disposition", "warn" if longer else "ok", "Kaybedeni tutma, kazananı erken satma", txt, d["n_win"] + d["n_loss"],
            {"median_win_days": mw, "median_loss_days": ml, "n_win": d["n_win"], "n_loss": d["n_loss"],
             "open_held_long": [x["symbol"] for x in d["open_held_long"]]},
            "Zarardaki bir pozisyonu tutmaya devam ederken bunu yeni bir tezle mi yapıyorsun, yoksa başa baş noktasını mı bekliyorsun?")

    pl = r["post_loss"]
    a, b = pl["after"], pl["base"]
    if a["n"]:
        worse = _worse(a["exp"], b["exp"])
        txt = (f"Bir zararlı işlem kapandıktan sonraki {pl['window']} gün içinde açılan {a['n']} işlemde isabet {_p(a['hit'], 0, False)}, "
               f"işlem başı beklenti {_usd(a['exp'])} ({_p(a['exp_pct'])}); diğer {b['n']} işlemde isabet {_p(b['hit'], 0, False)}, beklenti {_usd(b['exp'])} ({_p(b['exp_pct'])}).")
        if pl["cost_ratio"]:
            txt += f" Bu işlemlerin medyan büyüklüğü tüm işlemlerin medyanının {tr_n(pl['cost_ratio'], 2)} katı."
        if pl["same_symbol"]:
            txt += f" {pl['same_symbol']} tanesi aynı hisseye hemen geri dönüş."
        add("post_loss", "warn" if worse else "ok", "Zarardan hemen sonra işlem (hırs / intikam kontrolü)", txt, a["n"],
            {"after_n": a["n"], "after_hit": a["hit"], "after_exp": a["exp"], "after_exp_pct": a["exp_pct"], "base_n": b["n"],
             "base_hit": b["hit"], "base_exp": b["exp"], "base_exp_pct": b["exp_pct"], "cost_ratio": pl["cost_ratio"],
             "same_symbol": pl["same_symbol"]},
            "Zararla kapanan bir işlemden sonraki ilk alımda, plan önceden yazılı mıydı yoksa kaybı telafi etme isteği mi vardı?")

    for key, title, q in (("avg_down", "Ortalama düşürme (düşüşte ekleme)",
                           "Düşüşte eklerken bunu önceden yazılmış bir plana göre mi yaptın, yoksa maliyeti düşürmek için mi?"),
                          ("add_up", "Kazanana ekleme (yükselişte ekleme)",
                           "Kazanan pozisyona eklerken risk (stop / pozisyon büyüklüğü) yeniden hesaplandı mı?")):
        s = r[key]
        y, n_ = s["yes"], s["no"]
        if not y["n"]:
            continue
        worse = _worse(y["exp"], n_["exp"])
        txt = (f"{y['n']} işlemde ({s['events']} ekleme) isabet {_p(y['hit'], 0, False)}, beklenti {_usd(y['exp'])} ({_p(y['exp_pct'])}), toplam {_usd(y['sum'])}; "
               f"eklemesiz {n_['n']} işlemde isabet {_p(n_['hit'], 0, False)}, beklenti {_usd(n_['exp'])} ({_p(n_['exp_pct'])}), toplam {_usd(n_['sum'])}.")
        add(key, ("warn" if worse else "ok") if key == "avg_down" else "info", title, txt, y["n"],
            {"yes_n": y["n"], "yes_hit": y["hit"], "yes_exp": y["exp"], "yes_exp_pct": y["exp_pct"], "yes_sum": y["sum"],
             "no_n": n_["n"], "no_hit": n_["hit"], "no_exp": n_["exp"], "no_exp_pct": n_["exp_pct"], "no_sum": n_["sum"]}, q)

    ae = r["after_exit"]
    h0, h1 = ae["horizons"][0], ae["horizons"][-1]
    gw, gl = ae["groups"]["win"], ae["groups"]["loss"]
    if gw[h0]["n"] or gl[h0]["n"]:
        parts = []
        if gw[h0]["n"]:
            parts.append(f"Kârla satılan {gw[h0]['n']} işlemde fiyat {h0} işlem günü sonra satış fiyatına göre medyan {_p(gw[h0]['median'])} "
                         f"(satış fiyatının üstünde olan pay %{tr_n(gw[h0]['up_share'], 0)})"
                         + (f", {h1} gün sonra medyan {_p(gw[h1]['median'])} (n={gw[h1]['n']})" if gw[h1]["n"] else "") + ".")
        if gl[h0]["n"]:
            parts.append(f"Zararla satılan {gl[h0]['n']} işlemde {h0} gün sonra medyan {_p(gl[h0]['median'])} "
                         f"(satış fiyatının üstüne dönen pay %{tr_n(gl[h0]['up_share'], 0)})"
                         + (f", {h1} gün sonra medyan {_p(gl[h1]['median'])} (n={gl[h1]['n']})" if gl[h1]["n"] else "") + ".")
        early = gw[h0]["median"] is not None and gw[h0]["median"] > 5
        add("after_exit", "warn" if early else "info", "Satıştan sonra fiyat ne yaptı?", " ".join(parts), gw[h0]["n"] + gl[h0]["n"],
            {f"win_d{h}_median": gw[h]["median"] for h in ae["horizons"]} | {f"loss_d{h}_median": gl[h]["median"] for h in ae["horizons"]}
            | {f"win_d{h}_n": gw[h]["n"] for h in ae["horizons"]} | {f"loss_d{h}_n": gl[h]["n"] for h in ae["horizons"]},
            "Çıkışlar önceden belirlenmiş bir kurala (hedef, stop, tez bozulması) mı dayanıyordu, yoksa o günkü hisse mi?")

    hb = [b for b in r["holding"] if b["n"]]
    if len(hb) >= 2:
        best = max(hb, key=lambda b: b["exp"] if b["exp"] is not None else -1e18)
        worst = min(hb, key=lambda b: b["exp"] if b["exp"] is not None else 1e18)
        txt = (f"En iyi sonuç {best['label']} tutulan işlemlerde (n={best['n']}, isabet {_p(best['hit'], 0, False)}, beklenti {_usd(best['exp'])}); "
               f"en zayıf {worst['label']} (n={worst['n']}, isabet {_p(worst['hit'], 0, False)}, beklenti {_usd(worst['exp'])}).")
        add("holding", "info", "Elde tutma süresi", txt, sum(b["n"] for b in hb),
            {b["label"]: {"n": b["n"], "hit": b["hit"], "exp": b["exp"], "sum": b["sum"]} for b in hb},
            "Kısa süreli işlemlerin kararları aynı özenle (tez, çıkış kuralı) mı alınıyor?")

    sz = r["sizing"]
    if sz["n"]:
        big = sz["big"]
        txt = f"Medyan işlem büyüklüğü: portföyün {_p(sz['median'], 1, False)} kadarı. {big['n']} işlem %{tr_n(sz['flag'], 0)} eşiğini aşmış"
        yrs = [f"{y['year']}: {y['over']}" for y in sz["by_year"] if y["over"]]
        if yrs:
            txt += f" (yıllara göre {', '.join(yrs)})"
        if big["n"]:
            txt += f": isabet {_p(big['hit'], 0, False)}, beklenti {_usd(big['exp'])}, toplam {_usd(big['sum'])} (diğerleri beklenti {_usd(sz['rest']['exp'])})."
        else:
            txt += "."
        add("sizing", "warn" if big["n"] and big["sum"] < 0 else "info", "Pozisyon büyüklüğü", txt, sz["n"],
            {"median_size_pct": sz["median"], "over_flag_n": big["n"], "over_flag_exp": big["exp"], "over_flag_sum": big["sum"],
             "rest_exp": sz["rest"]["exp"]},
            "Büyük pozisyonlarda inanç seviyesi ve çıkış kuralı küçüklerden daha net miydi?")

    mo = r["monthly"]
    if mo["busy"]["n"]:
        worse = _worse(mo["busy"]["exp"], mo["calm"]["exp"])
        txt = (f"Ayda medyan {tr_n(mo['median_n'], 0)} işlem kapanmış; {len(mo['busy_months'])} yoğun ayda (≥{tr_n(mo['threshold'], 0)} işlem) "
               f"{mo['busy']['n']} işlem: isabet {_p(mo['busy']['hit'], 0, False)}, beklenti {_usd(mo['busy']['exp'])}; "
               f"diğer aylarda beklenti {_usd(mo['calm']['exp'])}.")
        add("overtrading", "warn" if worse else "info", "Yoğun işlem ayları", txt, mo["busy"]["n"],
            {"busy_months": mo["busy_months"], "busy_n": mo["busy"]["n"], "busy_exp": mo["busy"]["exp"], "calm_exp": mo["calm"]["exp"],
             "threshold": mo["threshold"]},
            "Yoğun aylarda işlem sayısını artıran şey yeni fırsatlar mıydı, yoksa piyasayı kaçırma korkusu mu?")
    order = {"warn": 0, "info": 1, "ok": 2}
    out.sort(key=lambda f: order[f["tone"]])
    return out


def summary(r: dict) -> str:
    o = r["overall"]
    s = (f"{r['first_close']} – {r['last_close']} arasında {o['n']} kapanan işlem: isabet {_p(o['hit'], 0, False)}, "
         f"işlem başı beklenti {_usd(o['exp'])} ({_p(o['exp_pct'])}), kâr faktörü {tr_n(o['profit_factor'], 2)}, toplam {_usd(o['sum'])}.")
    warn = [f for f in r["findings"] if f["tone"] == "warn"]
    if warn:
        s += " Dikkat çeken örüntü: " + ", ".join(f["title"].split(" (")[0].lower() for f in warn[:2]) + "."
    else:
        s += " Belirgin bir olumsuz davranış örüntüsü görünmüyor."
    return s


# ── orchestration ───────────────────────────────────────────

def analyze(trips: list[dict], *, open_pos: list[dict] | None = None, total: list[dict] | None = None,
            series: dict | None = None, as_of: str | None = None) -> dict:
    """Pure: all sections from already-loaded data."""
    trips = [t for t in trips if t.get("close")]
    if not trips:
        return {"as_of": as_of, "empty": True}
    sz = sizing(trips, total or [])
    sizes = sz.pop("_sizes")
    r = {
        "as_of": as_of, "n": len(trips),
        "first_close": min(t["close"] for t in trips), "last_close": max(t["close"] for t in trips),
        "first_open": min(t["open"] for t in trips),
        "overall": stats(trips),
        "holding": holding_buckets(trips),
        "disposition": disposition(trips, open_pos or [], as_of),
        "post_loss": post_loss(trips, sizes),
        "avg_down": split_by(trips, "avg_down"),
        "add_up": split_by(trips, "add_up"),
        "after_exit": after_exit(trips, series or {}),
        "sizing": sz,
        "monthly": monthly(trips),
        "by_symbol": by_symbol(trips),
        "limits": {"small_n": SMALL_N, "post_loss_days": POST_LOSS_DAYS, "size_flag": SIZE_FLAG, "drift": list(DRIFT)},
    }
    r["findings"] = findings(r)
    r["summary"] = summary(r)
    return r


def compute(max_age: float = 600) -> dict:
    from . import portfolio, prices
    led = portfolio.build()
    key = (led.days[-1] if led.days else None, len(led.closed), round(led.total[-1]["value"], 2) if led.total else 0)
    hit = _CACHE.get("r")
    if hit and hit[0] == key and time.time() - hit[1] < max_age:
        return hit[2]
    trips = portfolio.closed_positions(led)
    first = min((t["close"] for t in trips), default=None)
    series = prices.series_many({t["symbol"] for t in trips}, (_d(first) - timedelta(days=5)).isoformat()) if first else {}
    as_of = led.days[-1] if led.days else None
    r = analyze(trips, open_pos=portfolio.positions(led=led), total=led.total, series=series, as_of=as_of)
    r["sources"] = ["radar defteri (işlemler)", "Yahoo günlük kapanış"]
    _CACHE["r"] = (key, time.time(), r)
    return r


def brief(r: dict | None) -> dict | None:
    """Compact JSON for context snapshots / Hermes."""
    if not r or r.get("empty"):
        return None
    rd = lambda v, d=2: None if v is None else round(v, d)  # noqa: E731
    o = r["overall"]
    ae = r["after_exit"]["groups"]
    return {
        "as_of": r["as_of"], "period": [r["first_close"], r["last_close"]], "trips": o["n"],
        "hit_pct": rd(o["hit"], 1), "avg_win_usd": rd(o["avg_win"], 0), "avg_loss_usd": rd(o["avg_loss"], 0),
        "avg_win_pct": rd(o["avg_win_pct"], 1), "avg_loss_pct": rd(o["avg_loss_pct"], 1), "payoff": rd(o["payoff"]),
        "profit_factor": rd(o["profit_factor"]), "expectancy_usd": rd(o["exp"], 0), "expectancy_pct": rd(o["exp_pct"], 1),
        "realized_usd": rd(o["sum"], 0),
        "holding": [{"bucket": b["label"], "n": b["n"], "hit_pct": rd(b["hit"], 0), "exp_usd": rd(b["exp"], 0), "sum_usd": rd(b["sum"], 0)}
                    for b in r["holding"]],
        "median_hold_days": {"win": r["disposition"]["median_win_days"], "loss": r["disposition"]["median_loss_days"]},
        "open_losers_held_long": [x["symbol"] for x in r["disposition"]["open_held_long"]],
        "post_loss": {"n": r["post_loss"]["after"]["n"], "hit_pct": rd(r["post_loss"]["after"]["hit"], 0),
                      "exp_usd": rd(r["post_loss"]["after"]["exp"], 0), "base_exp_usd": rd(r["post_loss"]["base"]["exp"], 0)},
        "avg_down": {"n": r["avg_down"]["yes"]["n"], "exp_usd": rd(r["avg_down"]["yes"]["exp"], 0), "sum_usd": rd(r["avg_down"]["yes"]["sum"], 0),
                     "none_exp_usd": rd(r["avg_down"]["no"]["exp"], 0)},
        "after_exit_median_pct": {f"{g}_d{h}": rd(ae[g][h]["median"], 1) for g in ("win", "loss") for h in r["after_exit"]["horizons"]},
        "size_over_20pct": r["sizing"]["big"]["n"], "median_size_pct": rd(r["sizing"]["median"], 1),
        "busy_months": r["monthly"]["busy_months"],
        "top_symbols": [{"symbol": x["symbol"], "usd": rd(x["realized"], 0)} for x in r["by_symbol"]["top"]],
        "bottom_symbols": [{"symbol": x["symbol"], "usd": rd(x["realized"], 0)} for x in r["by_symbol"]["bottom"]],
        "findings": [{"key": f["key"], "tone": f["tone"], "text": f["text"], "n": f["n"], "small_sample": f["note"] is not None,
                      "question": f["question"]} for f in r["findings"]],
        "summary": r["summary"],
        "note": "Süreç analizi; alım/satım önerisi değildir.",
    }
