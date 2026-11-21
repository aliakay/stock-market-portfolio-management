"""Stress tests of the strategy lab: each test checked on a small hand-computed case, then one synthetic end-to-end run."""

import json
import math

import numpy as np
import pytest

from borsa_radar import lab, lab_stress


def test_rules_are_pinned():
    """The judge's thresholds were written before any run. Changing one must be a deliberate edit of this test too."""
    assert lab_stress.RULES == {
        "version": 1,
        "fixed_on": "2026-11-17",
        "fee": {"costs_rt_pct": [0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0], "survive_x": 3},
        "starts": {"from": 20060101, "horizon": 252, "step": 1, "median_min": 0.0, "worst_min": -20.0},
        "rank": {"th": [5, 10, 15, 20, 25], "limit": [0.02, 0.04, 0.06, 0.08, 0.10], "hold": [10, 20, 30, 40],
                 "macro": ["none", "all"], "min_trades": 100, "top_share": 0.25},
        "random": {"draws": 2000, "seed": 20261117, "pct_min": 95.0, "z_floor": 1.645},
    }


def test_deflated_bar_rises_with_attempts():
    assert lab_stress.deflated_bar(1) == 1.645
    assert lab_stress.deflated_bar(700) == pytest.approx(math.sqrt(2 * math.log(700)))   # 3.62
    assert lab_stress.deflated_bar(10_000) > lab_stress.deflated_bar(700)


def test_fee_break_even_and_pass():
    rows = [{"cost": c, "avg": a, "cagr": 5.0} for c, a in [(0.0, 0.4), (0.1, 0.3), (0.2, 0.2), (0.3, 0.1), (0.5, -0.1)]]
    f = lab_stress.fee_test(rows)
    assert f["break_even"] == pytest.approx(0.4)          # 0.3 + 0.1 / 0.2 × 0.2
    assert f["need_cost"] == pytest.approx(0.3) and f["pass"]
    rows[3]["cagr"] = -1.0                                  # the book shrinks at 3× cost → fails
    assert not lab_stress.fee_test(rows)["pass"]
    assert lab_stress.fee_test([{"cost": 0.0, "avg": -0.1, "cagr": 1.0}])["break_even"] == 0.0


def test_start_dates_median_and_worst():
    days = np.array([20100104, 20100105, 20100106, 20100107])
    s = lab_stress.start_test(np.array([5.0, -10.0, 20.0, 3.0]), np.array([2.0, 12.0, 3.0, 4.0]), np.array([4.0, 1.0, 10.0, 6.0]), days)
    assert s["median"] == 4.0 and s["worst"] == -10.0 and s["worst_day"] == 20100105 and s["pass"]
    assert s["share_beat_spy"] == 50.0 and s["worst_dd"] == 12.0
    s2 = lab_stress.start_test(np.array([5.0, -25.0, 20.0, 3.0]), np.zeros(4), np.zeros(4), days)
    assert not s2["pass"]                                   # one start lost more than 20 %


def test_rank_top_quarter_on_both_halves():
    def row(trw, tew, n=150):
        return {"train": {"n": n, "win": trw, "avg": 1.0}, "test": {"n": n, "win": tew, "avg": 1.0}}
    table = {"A": row(90, 70), "L": row(85, 88), "C": row(80, 80), "D": row(75, 79), "E": row(70, 75), "F": row(65, 74),
             "G": row(60, 73), "H": row(55, 72), "few": row(99, 99, n=50)}       # "few" has too few trades: not ranked
    r = lab_stress.rank_test("L", table)
    assert (r["train_rank"], r["train_of"], r["test_rank"], r["top_train"]) == (2, 8, 1, 2) and r["pass"]
    assert r["winner"] == "A" and r["winner_test_rank"] == 8
    assert not lab_stress.rank_test("C", table)["pass"]                      # 3rd of 8 on train: outside the top quarter


def test_random_baseline_hand_case():
    """Every random draw is the same trade: B2 return = 110/100 − 1 − cost; zero spread → no z → no pass."""
    opens = np.array([100.0, 110.0, 121.0, 133.1])
    r = lab_stress.random_test(np.full(25, 0.02), np.ones(25, dtype=np.int64), np.full(30, 0.01), opens,
                               np.zeros(30, dtype=np.int64), 600, 1)
    assert r["random_hold"]["median"] == pytest.approx((0.1 - lab.COST_RT) * 100)
    assert r["same_exit"]["median"] == pytest.approx(1.0) and r["same_exit"]["pct"] == 100.0
    assert r["same_exit"]["z"] is None and not r["pass"]


def test_random_baseline_separates_edge_from_noise(monkeypatch):
    monkeypatch.setitem(lab_stress.RULES, "random", {**lab_stress.RULES["random"], "draws": 400})
    rng = np.random.default_rng(7)
    pool = rng.normal(0.0, 0.05, 20000)
    opens = np.cumprod(1 + rng.normal(0.0, 0.01, 50000)) * 50
    idx = np.arange(0, 49000)
    holds = np.full(500, 5, dtype=np.int64)
    noise = lab_stress.random_test(rng.choice(pool, 500), holds, pool, opens, idx, 600, 2)
    edge = lab_stress.random_test(rng.choice(pool, 500) + 0.03, holds, pool, opens, idx, 600, 3)
    assert not noise["pass"] and noise["same_exit"]["pct"] < 99
    assert edge["pass"] and edge["same_exit"]["z"] > edge["bar"] > 3.5 and edge["random_hold"]["pct"] == 100.0


def test_start_replay_matches_the_book(monkeypatch):
    monkeypatch.setitem(lab_stress.RULES, "starts", {**lab_stress.RULES["starts"], "horizon": 5, "from": 0})
    cal = np.arange(20200101, 20200111, dtype=np.int64)
    closes = {"A": (cal, np.array([10, 10, 10, 10.5, 10.8, 11, 11, 11, 11, 11], dtype=np.float32))}
    t = lab.Trade("A", int(cal[1]), int(cal[2]), 10.0, int(cal[5]), 11.0, 0.099, 3, 1.0, "signal")
    spy = (cal, np.full(10, 100.0), np.linspace(100, 109, 10))
    s = lab_stress._book_starts([t], closes, cal, spy, slots=1)
    assert s["n"] == 6
    for i, (day, ret, _) in enumerate(s["series"]):
        c = cal[i:i + 5]
        direct = lab.portfolio([t] if c[0] <= t.entry_day <= c[-1] else [], closes, c, slots=1, start=int(c[0]))
        assert ret == pytest.approx((direct["final"] - 1) * 100, abs=0.01)
    # start on the entry day: in at 10 (+half cost), out at 11 (−half cost) inside the window
    assert s["series"][2][1] == pytest.approx((11 * (1 - lab.COST_RT / 2) / (10 * (1 + lab.COST_RT / 2)) - 1) * 100, abs=0.01)


def test_grid_is_the_live_family():
    from borsa_radar import strateji
    live = [s.spec for s in strateji.STRATEGIES]
    g = lab_stress.grid_specs(live)
    assert len(g) == 5 * 5 * 4 * 2
    keys = {s.key for s in g}
    assert all(s.key in keys for s in live)
    assert all(s.entry == "crsi" and s.exit == "above_entry" and s.mode == "limit" for s in g)


@pytest.fixture
def lab_db(tmp_path, monkeypatch):
    from borsa_radar import db
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    return db


def test_attempts_and_journal_flags(lab_db):
    assert lab_stress.record_attempts(["a", "b"], "t") == 2
    assert lab_stress.record_attempts(["b", "c"], "t") == 1 and lab_stress.attempt_count() == 3
    for h in ("aaa", "aaa", "bbb"):
        lab_db.execute("INSERT INTO lab_journal(created_at, rules_hash, code_hash, attempts, body) VALUES ('2026-11-17', ?, 'c1', 3, '{}')", (h,))
    j = lab_stress.journal()
    assert j[0]["rules_changed"] and not j[1]["rules_changed"] and not j[0]["code_changed"]


class FakeHist:
    """Stands in for the npz archive: .files and [key]."""

    def __init__(self, arrays: dict):
        self.d = arrays
        self.files = list(arrays)

    def __getitem__(self, k):
        return self.d[k]


def _days(n, start="2005-01-03"):
    d = np.busday_offset(np.datetime64(start), np.arange(n), roll="forward")
    s = d.astype("datetime64[D]").astype(str)
    return np.array([int(x.replace("-", "")) for x in s], dtype=float)


def _stock(seed, n):
    """Up-drifting walk with a sharp three-day sell-off every ~45 sessions and a deep intraday flush the day after."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0025, 0.008, n)
    for k in range(260, n - 5, 45):
        r[k:k + 3] = -0.025
    c = 40 * np.cumprod(1 + r)
    o = np.concatenate([[c[0]], c[:-1]]) * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * 1.005
    lo = np.minimum(o, c) * 0.995
    for k in range(260, n - 5, 45):
        lo[k + 3] = c[k + 2] * 0.92
    return np.column_stack([_days(n), o, h, lo, c, np.full(n, 5e6)])


def test_end_to_end_on_synthetic_history(lab_db, monkeypatch):
    from borsa_radar import strateji
    monkeypatch.setitem(lab_stress.RULES, "starts", {**lab_stress.RULES["starts"], "step": 60})
    monkeypatch.setitem(lab_stress.RULES, "random", {**lab_stress.RULES["random"], "draws": 200})
    monkeypatch.setitem(lab_stress.RULES, "rank", {**lab_stress.RULES["rank"], "min_trades": 5})
    n = 3600                                                      # 2005 → 2018: both halves present
    spy = np.column_stack([_days(n), np.linspace(100, 300, n), np.linspace(101, 301, n), np.linspace(99, 299, n),
                           np.linspace(100, 300, n), np.full(n, 1e8)])
    hist = FakeHist({"SPY": spy, "AAA": _stock(1, n), "BBB": _stock(2, n), "CCC": _stock(3, n)})
    res = lab_stress.run(hist=hist)
    assert [s["code"] for s in res["strategies"]] == [s.code for s in strateji.STRATEGIES]
    for s in res["strategies"]:
        t = s["tests"]
        assert set(t) == {"fee", "starts", "rank", "random"}
        assert s["passed"] == (not s["failed"]) and set(s["failed"]) <= set(t)
        assert len(t["fee"]["rows"]) == 9 and t["starts"]["n"] > 0 and t["rank"]["grid"] == 200
        assert t["fee"]["rows"][0]["n"] > 0                      # the synthetic sell-offs produced trades
    assert res["attempts"] >= 200 and lab_stress.verdict("derin")["passed"] == res["strategies"][0]["passed"]
    assert len(lab_stress.journal()) == 1
    json.dumps(res)                                               # stored as JSON


def test_page_shows_the_stress_section(lab_db, monkeypatch):
    from fastapi.testclient import TestClient

    from borsa_radar import strateji
    from borsa_radar.web.app import app
    st = {"n": 600, "win": 80.8, "avg": 2.17, "avg_win": 4.08, "avg_loss": -5.85, "pf": 2.94, "worst": -32.9, "p5": -7.8, "hold": 4.6,
          "years": {"2017": {"n": 20, "win": 85.0, "avg": 1.8}}}
    book = {"cagr": 7.1, "max_dd": 14.2, "exposure": 47.0, "taken": {"n": 490, "win": 78.4}, "curve": [[20170131, 1.0], [20170228, 1.02], [20170331, 1.05]]}
    spy = {"cagr": 14.6, "max_dd": 33.7, "curve": [[20170131, 1.0], [20170228, 1.03], [20170331, 1.04]]}
    row = {"code": "derin", "name": "Derin düşüş alımı", "key": "crsi|15|above_entry|limit6|h30|all", "what": "", "entry_tr": "", "exit_tr": "", "all": st}
    for w in ("train", "test"):
        row.update({w: st, w + "_stress": st, w + "_book": book, w + "_spy": spy})
    lab_db.set_cursor(strateji.RESULTS_KEY, json.dumps({"created_at": "2026-11-17T12:00:00+00:00", "data_through": "2026-11-16", "universe": 907,
                                                        "cost_rt": 0.1, "train_end": "2016-12-31", "strategies": [row]}))
    tests = {
        "fee": lab_stress.fee_test([{"cost": c, "n": 600, "win": 80.0, "avg": 2.0 - c * 4, "pf": 2.0, "cagr": 7.0 - c * 10}
                                    for c in lab_stress.RULES["fee"]["costs_rt_pct"]]),
        "starts": lab_stress.start_test(np.array([5.0, -8.0, 12.0]), np.array([3.0, 9.0, 4.0]), np.array([10.0, -5.0, 15.0]),
                                        np.array([20080102, 20080103, 20080104])),
        "rank": {"train_rank": 30, "train_of": 180, "test_rank": 12, "test_of": 170, "top_train": 45, "top_test": 43, "winner": "w",
                 "winner_test_rank": 2, "grid": 200, "pass": True,
                 "rows": [{"key": "crsi|15|above_entry|limit6|h30|all", "train": {"n": 220, "win": 85.0, "avg": 2.0},
                           "test": {"n": 600, "win": 80.8, "avg": 2.17}, "rt": 30, "re": 12}]},
        "random": {"n": 600, "real_avg": 2.17, "real_win": 80.8, "attempts": 806, "bar": 3.66, "pass": False,
                   "same_exit": {"pct": 91.0, "z": 1.3, "median": 1.6, "p95": 2.3, "win_pct": 80.0, "win_median": 77.0, "pool": 5400, "pass": False},
                   "random_hold": {"pct": 100.0, "z": 6.0, "median": 0.9, "p95": 1.3, "win_pct": 100.0, "win_median": 58.0, "pool": 900000, "pass": True}},
    }
    lab_db.set_cursor(lab_stress.CURSOR, json.dumps({
        "created_at": "2026-11-17T15:00:00+00:00", "data_through": "2026-11-16", "rules": lab_stress.RULES, "rules_hash": "r", "code_hash": "c",
        "attempts": 806, "bar": 3.66,
        "strategies": [{"code": "derin", "name": "Derin düşüş alımı", "key": row["key"], "tests": tests, "passed": False, "failed": ["random"]}]}))
    lab_db.execute("INSERT INTO lab_journal(created_at, rules_hash, code_hash, attempts, body) VALUES (?,?,?,?,?)",
                   ("2026-11-17T15:00:00+00:00", "r", "c", 806,
                    json.dumps({"data_through": "2026-11-16", "verdicts": {"derin": {"name": "Derin düşüş alımı", "passed": False, "failed": ["random"]}}})))
    t = TestClient(app).get("/setups/strateji").text
    assert "Stres testleri" in t and "Reddedildi" in t and "Rastgele girişe karşı testinden geçemedi" in t
    assert "Başa baş maliyet" in t and "806" in t and "<line" in t
    assert lab_stress.verdict("derin") == {"passed": False, "failed": ["random"], "day": "2026-11-17"}
