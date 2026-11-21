"""Stres testleri: a lab strategy counts only if it survives four fixed tests (reject by default).

1. Fee sweep (maliyet taraması) — the 2017 → rule re-run with round-trip costs from 0 to 2 %. Output: the break-even
   cost. Pass: at 3× the realistic cost the average net trade is still positive and the 10-slot book still grows.
2. Every start date (her başlangıç günü) — the 10-slot book started on every session since 2006 and run for 252
   sessions. Pass: the median 12-month result is positive and the worst start loses no more than 20 %.
3. Train vs unseen (seçim / sınav sırası) — a fixed grid of neighbouring parameter sets of the same rule family, ranked
   by win rate (the lab's selection objective) on 2006-2016 and on 2017 →. Pass: the live parameters sit in the top
   quarter on both halves.
4. Random baseline (rastgele giriş) — thousands of random strategies with the same number of trades on the same bars:
   a) same order and exit rule on random eligible days (does the entry signal add anything?),
   b) random entries in the same filtered universe, held exactly as long as the real trades (does the whole rule beat
      chance?). Pass: the rule's average net trade beats ≥ 95 % of both and its z-score clears √(2 ln N), N = distinct
      parameter sets the lab has ever tried — the best of N pure-noise strategies scores about that high by luck.

RULES is fixed before any run. Its hash and the hash of the backtester + this file go into the journal on every run;
the page flags a change. Tests in tests/test_lab_stress.py pin the thresholds, so loosening one is a visible edit.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from . import db, lab, lab_data
from .util import utcnow_iso

CURSOR = "lab_stress"
TEST_FROM = lab.TRAIN_END + 1

RULES = {
    "version": 1,
    "fixed_on": "2026-11-17",
    "fee": {"costs_rt_pct": [0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0], "survive_x": 3},
    "starts": {"from": 20060101, "horizon": 252, "step": 1, "median_min": 0.0, "worst_min": -20.0},
    "rank": {"th": [5, 10, 15, 20, 25], "limit": [0.02, 0.04, 0.06, 0.08, 0.10], "hold": [10, 20, 30, 40],
             "macro": ["none", "all"], "min_trades": 100, "top_share": 0.25},
    "random": {"draws": 2000, "seed": 20261117, "pct_min": 95.0, "z_floor": 1.645},
}

TEST_NAMES = {"fee": "Maliyet taraması", "starts": "Her başlangıç günü", "rank": "Seçim / sınav sırası",
              "random": "Rastgele girişe karşı"}


# ── bookkeeping: hashes, attempts, journal ─────────────────

def rules_hash() -> str:
    return hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()[:12]


def code_hash() -> str:
    h = hashlib.sha256()
    for f in (Path(lab.__file__), Path(__file__)):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def record_attempts(keys, source: str) -> int:
    """Remember parameter sets as tried (idempotent). Returns how many were new."""
    now = utcnow_iso()
    new = 0
    with db.tx() as c:
        for k in keys:
            new += c.execute("INSERT OR IGNORE INTO lab_attempts(key, first_seen, source) VALUES (?,?,?)", (k, now, source)).rowcount
    return new


def attempt_count() -> int:
    r = db.query_one("SELECT COUNT(*) n FROM lab_attempts")
    return int(r["n"]) if r else 0


def deflated_bar(n_attempts: int) -> float:
    """Expected best z-score among n independent noise strategies (Bailey & López de Prado): √(2 ln N)."""
    return max(RULES["random"]["z_floor"], math.sqrt(2 * math.log(n_attempts)) if n_attempts > 1 else 0.0)


def journal(limit: int = 12) -> list[dict]:
    rows = db.query("SELECT * FROM lab_journal ORDER BY id DESC LIMIT ?", (limit + 1,))
    out = []
    for i, r in enumerate(rows[:limit]):
        d = dict(r) | {"body": db.loads(r["body"], {})}
        prev = rows[i + 1] if i + 1 < len(rows) else None
        d["rules_changed"] = bool(prev and prev["rules_hash"] != r["rules_hash"])
        d["code_changed"] = bool(prev and prev["code_hash"] != r["code_hash"])
        out.append(d)
    return out


def latest() -> dict | None:
    return db.loads(db.get_cursor(CURSOR), None)


# ── the grid of neighbours for the rank test ───────────────

def grid_specs(live: list[lab.Spec]) -> list[lab.Spec]:
    """Every parameter set of each live strategy's family (same entry and exit rule), the live sets included."""
    g = RULES["rank"]
    out: dict[str, lab.Spec] = {}
    for s in live:
        limits = [(("limit", lim)) for lim in g["limit"]] if s.mode == "limit" else [("open", 0.0)]
        for th in g["th"]:
            for mode, lim in limits:
                for hold in g["hold"]:
                    for mac in g["macro"]:
                        sp = lab.Spec("", s.entry, th, s.exit, mode=mode, limit=lim, hold=hold, macro=mac, stop=s.stop)
                        out.setdefault(sp.key, sp)
        out.setdefault(s.key, s)
    return list(out.values())


# ── one pass over the long history ─────────────────────────

def collect(hist, live: list[lab.Spec], progress=None, members: dict | None = None) -> dict:
    """Everything the four tests need, computed symbol by symbol (indicators built once per symbol). With `members`
    every signal, and every random-baseline day, is limited to days the stock was an index member."""
    keymap = {lab_data._unkey(k): k for k in hist.files}
    get = lambda sym: hist[keymap[sym]] if sym in keymap else None  # noqa: E731
    m = lab.build_macro(get)
    syms = [s for s in sorted(keymap) if s not in lab_data.MACRO and (members is None or s in members)]
    grid = grid_specs(live)
    costs = [c / 100 for c in RULES["fee"]["costs_rt_pct"]]
    aggs = {s.key: lab.Agg() for s in grid}
    trades = {(s.key, c): [] for s in live for c in costs}
    b1 = {s.key: [] for s in live}
    b2 = {s.key: [] for s in live}
    opens: list[np.ndarray] = []
    off = 0
    closes: dict = {}
    for i, sym in enumerate(syms):
        bars = get(sym)
        if bars is None or len(bars) < 260:
            continue
        x = lab.Ind(bars)
        mem = lab.member_mask(x.day, members[sym]) if members is not None else None
        for s in grid:
            aggs[s.key].add(lab.simulate(s, sym, x, m, member=mem))
        test = x.day >= TEST_FROM if mem is None else (x.day >= TEST_FROM) & mem
        idx = np.arange(x.n)
        had = False
        for s in live:
            for c in costs:
                tr = lab.simulate(s, sym, x, m, cost=c, member=mem)
                trades[(s.key, c)].extend(tr)
                had = had or bool(tr)
            with np.errstate(invalid="ignore"):
                base = x.liquid & x.up_trend & lab.macro_mask(s, x, m) & test
            # (a) random day, same order and exit: every eligible day whose order would have filled
            fill = np.zeros(x.n, dtype=bool)
            if s.mode == "limit":
                with np.errstate(invalid="ignore"):
                    fill[:-1] = x.l[1:] <= x.c[:-1] * (1 - s.limit)
            else:
                fill[:-1] = True
            ex = lab.exit_mask(s, x)
            for t in np.flatnonzero(base & fill):
                tr = lab.follow_one(s, sym, x, int(t), ex)
                if tr is not None and tr.why not in ("open", "exit_next"):
                    b1[s.key].append(tr.ret)
            # (b) random entry at the next open, any eligible day with room for the longest hold
            ok = base & (idx + 1 + s.hold <= x.n - 1)
            b2[s.key].append(off + np.flatnonzero(ok) + 1)
        if had:
            closes[sym] = (x.day, x.c.astype(np.float32))
        opens.append(x.o.astype(np.float64))
        off += x.n
        if progress and i % 100 == 0:
            progress(i, len(syms))
    spy = get("SPY")
    return {"aggs": aggs, "grid": grid, "trades": trades, "costs": costs, "closes": closes, "cal": m.day,
            "b1": {k: np.array(v, dtype=np.float64) for k, v in b1.items()},
            "b2": {k: (np.concatenate(v) if v else np.zeros(0, dtype=np.int64)) for k, v in b2.items()},
            "opens": np.concatenate(opens) if opens else np.zeros(0),
            "spy": (spy[:, 0].astype(np.int64), spy[:, 1], spy[:, 4])}


# ── the four tests (pure functions of collected data) ──────

def fee_test(rows: list[dict]) -> dict:
    """rows: per cost {cost (% round trip), n, win, avg (%), pf, cagr}. Break-even = cost where the average trade hits 0."""
    be = None
    for a, b in zip(rows, rows[1:]):
        if a["avg"] > 0 >= b["avg"]:
            be = a["cost"] + a["avg"] / (a["avg"] - b["avg"]) * (b["cost"] - a["cost"])
            break
    if rows and rows[0]["avg"] <= 0:
        be = 0.0
    need = lab.COST_RT * 100 * RULES["fee"]["survive_x"]
    at = next((r for r in rows if abs(r["cost"] - need) < 1e-9), None)
    ok = bool(at and at["avg"] > 0 and at["cagr"] > 0)
    return {"rows": rows, "break_even": be, "need_cost": need, "at_need": at, "pass": ok}


def start_test(rets: np.ndarray, dds: np.ndarray, spy: np.ndarray, days: np.ndarray) -> dict:
    """12-month book results for every start day."""
    r = RULES["starts"]
    if not len(rets):
        return {"n": 0, "pass": False}
    w = int(np.argmin(rets))
    med = float(np.median(rets))
    step = max(1, len(rets) // 600)
    return {"n": int(len(rets)), "worst": float(rets[w]), "worst_day": int(days[w]), "p10": float(np.percentile(rets, 10)),
            "median": med, "p90": float(np.percentile(rets, 90)), "best": float(rets.max()),
            "share_pos": float((rets > 0).mean() * 100), "share_beat_spy": float((rets > spy).mean() * 100),
            "spy_median": float(np.median(spy)), "spy_worst": float(spy.min()), "worst_dd": float(dds.max()),
            "series": [(int(days[i]), round(float(rets[i]), 2), round(float(spy[i]), 2)) for i in range(0, len(rets), step)],
            "pass": bool(med > r["median_min"] and rets[w] >= r["worst_min"])}


def rank_test(live_key: str, table: dict[str, dict]) -> dict:
    """table[key] = {"train": {n, win, avg}, "test": {...}}. Rank by win rate (ties: average trade), ≥ min_trades only."""
    g = RULES["rank"]

    def order(half):
        ok = [k for k, v in table.items() if v[half].get("n", 0) >= g["min_trades"]]
        return sorted(ok, key=lambda k: (-table[k][half]["win"], -table[k][half]["avg"]))

    tr, te = order("train"), order("test")
    rt = tr.index(live_key) + 1 if live_key in tr else None
    re_ = te.index(live_key) + 1 if live_key in te else None
    top_tr, top_te = math.ceil(g["top_share"] * len(tr)), math.ceil(g["top_share"] * len(te))
    winner = tr[0] if tr else None
    show = list(dict.fromkeys(te[:8] + [k for k in (live_key, winner) if k]))
    rows = [{"key": k, "train": table[k]["train"], "test": table[k]["test"],
             "rt": tr.index(k) + 1 if k in tr else None, "re": te.index(k) + 1 if k in te else None} for k in show]
    return {"train_rank": rt, "train_of": len(tr), "test_rank": re_, "test_of": len(te), "top_train": top_tr, "top_test": top_te,
            "winner": winner, "winner_test_rank": te.index(winner) + 1 if winner in te else None, "grid": len(table),
            "rows": rows, "pass": bool(rt and re_ and rt <= top_tr and re_ <= top_te)}


def _compare(real: float, draws: np.ndarray) -> dict:
    sd = float(draws.std())
    return {"pct": float((draws < real).mean() * 100), "z": (real - float(draws.mean())) / sd if sd > 0 else None,
            "median": float(np.median(draws)), "p95": float(np.percentile(draws, 95))}


def random_test(real: np.ndarray, holds: np.ndarray, pool_b1: np.ndarray, opens: np.ndarray, idx_b2: np.ndarray,
                n_attempts: int, seed: int) -> dict:
    """real: net returns of the rule's finished 2017 → trades; holds: their sessions in the market."""
    R = RULES["random"]
    n = len(real)
    bar = deflated_bar(n_attempts)
    out = {"n": n, "real_avg": float(real.mean() * 100) if n else None, "real_win": float((real > 0).mean() * 100) if n else None,
           "attempts": n_attempts, "bar": bar}
    if n < 20 or len(pool_b1) < 20 or len(idx_b2) < 20:
        return out | {"pass": False, "note": "örnek az"}
    rng = np.random.default_rng(seed)
    D = R["draws"]
    m1, w1 = np.empty(D), np.empty(D)
    m2, w2 = np.empty(D), np.empty(D)
    for lo in range(0, D, 100):
        k = min(100, D - lo)
        s1 = pool_b1[rng.integers(0, len(pool_b1), size=(k, n))]
        m1[lo:lo + k], w1[lo:lo + k] = s1.mean(axis=1), (s1 > 0).mean(axis=1)
        g = idx_b2[rng.integers(0, len(idx_b2), size=(k, n))]
        with np.errstate(invalid="ignore", divide="ignore"):
            s2 = opens[g + holds[None, :]] / opens[g] - 1 - lab.COST_RT
        s2 = np.where(np.isfinite(s2), s2, np.nan)
        m2[lo:lo + k], w2[lo:lo + k] = np.nanmean(s2, axis=1), np.nanmean(s2 > 0, axis=1)
    ra, rw = float(real.mean()), float((real > 0).mean())
    a = _compare(ra, m1) | {"win_pct": float((w1 < rw).mean() * 100), "win_median": float(np.median(w1) * 100), "pool": int(len(pool_b1))}
    b = _compare(ra, m2) | {"win_pct": float((w2 < rw).mean() * 100), "win_median": float(np.median(w2) * 100), "pool": int(len(idx_b2))}
    for d in (a, b):
        d["median"] *= 100
        d["p95"] *= 100
        d["pass"] = bool(d["pct"] >= R["pct_min"] and d["z"] is not None and d["z"] >= bar)
    return out | {"same_exit": a, "random_hold": b, "pass": a["pass"] and b["pass"]}


# ── the whole run ──────────────────────────────────────────

def _book_starts(trades: list[lab.Trade], closes: dict, cal: np.ndarray, spy: tuple, slots: int) -> dict:
    r = RULES["starts"]
    H = r["horizon"]
    tr = sorted(trades, key=lambda t: t.entry_day)
    ent = [t.entry_day for t in tr]
    sd, so, sc = spy
    i0 = int(np.searchsorted(cal, r["from"]))
    rets, dds, spys, days = [], [], [], []
    for i in range(i0, len(cal) - H + 1, r["step"]):
        c = cal[i:i + H]
        lo, hi = int(c[0]), int(c[-1])
        sub = tr[bisect.bisect_left(ent, lo):bisect.bisect_right(ent, hi)]
        pf = lab.portfolio(sub, closes, c, slots=slots, start=lo)
        a, b = np.searchsorted(sd, lo), np.searchsorted(sd, hi, side="right") - 1
        rets.append((pf["final"] - 1) * 100)
        dds.append(pf["max_dd"])
        spys.append((sc[b] / so[a] - 1) * 100)
        days.append(lo)
    return start_test(np.array(rets), np.array(dds), np.array(spys), np.array(days))


def _summ(a: lab.Agg, lo: int, hi: int = 99999999) -> dict:
    s = a.summary(lo, hi)
    return {"n": s.get("n", 0), "win": s.get("win", 0.0), "avg": s.get("avg", 0.0)}


def run(progress=None, hist=None, universe: str = "pit") -> dict:
    from . import strateji
    live = [s.spec for s in strateji.STRATEGIES]
    members = None
    mode = "given"
    if hist is None:
        mode, hist, members = lab_data.universe(universe)
    data = collect(hist, live, progress=progress, members=members)
    record_attempts([s.key for s in data["grid"]], "stres ızgarası")
    n_att = attempt_count()
    cal, closes = data["cal"], data["closes"]
    table = {s.key: {"train": _summ(data["aggs"][s.key], 20060101, lab.TRAIN_END), "test": _summ(data["aggs"][s.key], TEST_FROM)}
             for s in data["grid"]}
    out = []
    for k, st in enumerate(strateji.STRATEGIES):
        spec = st.spec
        rows = []
        for c in data["costs"]:
            te = [t for t in data["trades"][(spec.key, c)] if t.sig >= TEST_FROM]
            s = lab.stats(te)
            book = lab.portfolio(te, closes, cal[cal >= TEST_FROM], slots=strateji.BOOK_SLOTS, start=TEST_FROM, cost=c)
            rows.append({"cost": round(c * 100, 4), "n": s.get("n", 0), "win": s.get("win", 0.0), "avg": s.get("avg", 0.0),
                         "pf": s["pf"] if s.get("n") and math.isfinite(s["pf"]) else None, "cagr": float(book["cagr"])})
        base_cost = min(data["costs"], key=lambda c: abs(c - lab.COST_RT))
        base = data["trades"][(spec.key, base_cost)]
        done = [t for t in base if t.sig >= TEST_FROM and t.why not in ("open", "exit_next")]
        tests = {
            "fee": fee_test(rows),
            "starts": _book_starts(base, closes, cal, data["spy"], strateji.BOOK_SLOTS),
            "rank": rank_test(spec.key, table),
            "random": random_test(np.array([t.ret for t in done]), np.array([t.hold for t in done], dtype=np.int64),
                                  data["b1"][spec.key], data["opens"], data["b2"][spec.key], n_att, RULES["random"]["seed"] + k),
        }
        failed = [code for code, t in tests.items() if not t["pass"]]
        out.append({"code": st.code, "name": st.name, "key": spec.key, "tests": tests, "passed": not failed, "failed": failed})
    sd = data["spy"][0]
    res = {"created_at": utcnow_iso(), "data_through": f"{sd[-1] // 10000}-{sd[-1] // 100 % 100:02d}-{sd[-1] % 100:02d}",
           "rules": RULES, "rules_hash": rules_hash(), "code_hash": code_hash(), "attempts": n_att, "bar": deflated_bar(n_att),
           "universe_mode": mode, "strategies": out}
    db.set_cursor(CURSOR, json.dumps(res, ensure_ascii=False))
    db.execute("INSERT INTO lab_journal(created_at, rules_hash, code_hash, attempts, body) VALUES (?,?,?,?,?)",
               (res["created_at"], res["rules_hash"], res["code_hash"], n_att,
                json.dumps({"data_through": res["data_through"], "universe_mode": mode,
                            "verdicts": {s["code"]: {"name": s["name"], "passed": s["passed"], "failed": s["failed"]} for s in out}},
                           ensure_ascii=False)))
    return res


def verdict(code: str) -> dict | None:
    """Latest judge decision for one live strategy: {"passed", "failed", "day"} or None when never tested."""
    r = latest()
    if not r:
        return None
    s = next((x for x in r["strategies"] if x["code"] == code), None)
    return {"passed": s["passed"], "failed": s["failed"], "day": r["created_at"][:10]} if s else None
