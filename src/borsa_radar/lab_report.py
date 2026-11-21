"""Builds the stored backtest record of the lab strategies (`borsa-radar lab run`, a few minutes, no API quota).

Universe: point-in-time S&P 500 by default (`lab_data.universe`); "today" reproduces the older survivor-only run.

For every strategy in `strateji.STRATEGIES`: train (2006-2016) and test (2017 → today) trade statistics, the same
with doubled costs, a capped 10-slot portfolio replay for each window against SPY buy & hold, and a per-year table.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np

from . import db, lab, lab_data, strateji
from .util import utcnow_iso

WINDOWS = {"train": (20060101, lab.TRAIN_END), "test": (lab.TRAIN_END + 1, 99999999)}


def spy_bench(hist, lo: int, hi: int) -> dict:
    keymap = {lab_data._unkey(k): k for k in hist.files}
    a = hist[keymap["SPY"]]
    d = a[:, 0].astype(np.int64)
    k = (d >= lo) & (d <= hi)
    c, dd = a[:, 4][k], d[k]
    if len(c) < 2:
        return {}
    yrs = len(c) / 252
    peak = np.maximum.accumulate(c)
    curve: list = []
    for day, v in zip(dd, c / c[0]):
        if curve and curve[-1][0] // 100 == int(day) // 100:
            curve[-1] = (int(day), round(float(v), 4))
        else:
            curve.append((int(day), round(float(v), 4)))
    return {"cagr": ((c[-1] / c[0]) ** (1 / yrs) - 1) * 100, "max_dd": float((1 - c / peak).max() * 100), "curve": curve}


def _clean(st: dict) -> dict:
    """JSON-safe copy (inf profit factor → None)."""
    out = {}
    for k, v in st.items():
        if isinstance(v, float) and not np.isfinite(v):
            out[k] = None
        elif isinstance(v, dict):
            out[k] = {str(kk): vv for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def build(progress=None, universe: str = "pit", save: bool = True) -> dict:
    mode, hist, members = lab_data.universe(universe)
    specs = [s.spec for s in strateji.STRATEGIES]
    keys = {s.key for s in specs}
    aggs, kept, closes = lab.run(specs, hist=hist, keep=keys, progress=progress, members=members)
    stress, _, _ = lab.run(specs, hist=hist, cost=lab.COST_RT * 2, members=members)
    cal = closes.pop("__cal__")
    out = []
    for st in strateji.STRATEGIES:
        k = st.spec.key
        row = {"code": st.code, "name": st.name, "key": k, "what": st.what, "entry_tr": st.entry_tr, "exit_tr": st.exit_tr}
        for w, (lo, hi) in WINDOWS.items():
            row[w] = _clean(aggs[k].summary(lo, hi))
            row[w + "_stress"] = _clean({kk: v for kk, v in stress[k].summary(lo, hi).items() if kk != "years"})
            trades = [t for t in kept[k] if lo <= t.sig <= hi]
            pf = lab.portfolio(trades, closes, cal[(cal >= lo) & (cal <= hi)], slots=strateji.BOOK_SLOTS, start=lo)
            row[w + "_book"] = {"cagr": pf["cagr"], "max_dd": pf["max_dd"], "exposure": pf["exposure"],
                                "taken": _clean({kk: v for kk, v in pf["taken"].items() if kk != "years"}),
                                "curve": [(int(d), round(float(e), 4)) for d, e in pf["curve"]]}
            row[w + "_spy"] = spy_bench(hist, lo, hi)
        row["all"] = _clean(aggs[k].summary(20060101))
        out.append(row)
    spy_last = int(hist[{lab_data._unkey(k): k for k in hist.files}["SPY"]][-1, 0])
    names = [lab_data._unkey(f) for f in hist.files if lab_data._unkey(f) not in lab_data.MACRO]
    info = lab_data.members_info() if mode == "pit" else None
    res = {"created_at": utcnow_iso(), "data_through": f"{spy_last // 10000}-{spy_last // 100 % 100:02d}-{spy_last % 100:02d}",
           "universe": len([n for n in names if members is None or n in members]), "universe_mode": mode,
           "gap": {"status": info["status"], "csv_through": info["csv_through"], "names": info["names"]} if info else None,
           "cost_rt": lab.COST_RT * 100, "train_end": "2016-12-31", "strategies": out, "today": date.today().isoformat()}
    if save:
        db.set_cursor(strateji.RESULTS_KEY, json.dumps(res, ensure_ascii=False))
    return res
