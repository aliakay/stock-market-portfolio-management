import pytest

from borsa_radar import behavior
from borsa_radar.prices import Series


def trip(symbol, open_, close, realized, cost=1000.0, *, buys=1, avg_down=0, add_up=0, exit_px=10.0, max_cost=None):
    from datetime import date
    days = (date.fromisoformat(close) - date.fromisoformat(open_)).days
    return {"symbol": symbol, "asset": "stock", "sub": "P1", "open": open_, "close": close, "days": days, "cost": cost,
            "proceeds": cost + realized, "realized": realized, "pct": realized / cost * 100, "qty": 1, "buys": buys,
            "sells": 1, "entry_px": 10.0, "exit_px": exit_px, "avg_down": avg_down, "add_up": add_up,
            "max_cost": max_cost or cost}


def test_stats_core_numbers():
    ts = [trip("A", "2026-01-01", "2026-01-10", 200), trip("B", "2026-01-01", "2026-01-10", 100),
          trip("C", "2026-01-01", "2026-01-10", -100), trip("D", "2026-01-01", "2026-01-10", -50)]
    s = behavior.stats(ts)
    assert s["n"] == 4 and s["wins"] == 2 and s["losses"] == 2
    assert s["hit"] == pytest.approx(50)
    assert s["avg_win"] == pytest.approx(150) and s["avg_loss"] == pytest.approx(-75)
    assert s["payoff"] == pytest.approx(2)
    assert s["profit_factor"] == pytest.approx(300 / 150)
    assert s["exp"] == pytest.approx(150 / 4)
    assert s["exp_pct"] == pytest.approx(15 / 4)
    assert s["sum"] == pytest.approx(150)
    assert s["note"] == "küçük örneklem (n=4)"
    assert behavior.stats([])["hit"] is None


def test_holding_buckets_edges():
    ts = [trip("A", "2026-01-01", "2026-01-07", 10),    # 6 days
          trip("B", "2026-01-01", "2026-01-08", -10),   # 7 days → 7–30
          trip("C", "2026-01-01", "2026-03-02", 30),    # 60 days
          trip("D", "2025-01-01", "2026-01-01", 40)]    # 365 days
    b = {x["label"]: x for x in behavior.holding_buckets(ts)}
    assert b["<7 gün"]["n"] == 1 and b["7–30 gün"]["n"] == 1 and b["30–90 gün"]["n"] == 1 and b["90+ gün"]["n"] == 1
    assert b["7–30 gün"]["hit"] == 0


def test_disposition_and_open_losers():
    ts = [trip("W1", "2026-01-01", "2026-01-11", 50), trip("W2", "2026-01-01", "2026-01-21", 50),
          trip("L1", "2026-01-01", "2026-03-02", -50), trip("L2", "2026-01-01", "2026-04-11", -50)]
    pos = [{"symbol": "X", "unreal_pct": -10, "first_day": "2026-01-01", "weight": 5, "unreal": -100},
           {"symbol": "Y", "unreal_pct": 10, "first_day": "2025-01-01"},
           {"symbol": "Z", "unreal_pct": -5, "first_day": "2026-06-20"}]
    d = behavior.disposition(ts, pos, "2026-07-01")
    assert d["median_win_days"] == 15 and d["median_loss_days"] == 80
    assert [x["symbol"] for x in d["open_held_long"]] == ["X"]   # Y is winning, Z too young


def test_post_loss_window():
    ts = [trip("L", "2026-01-01", "2026-02-01", -100),
          trip("R", "2026-02-03", "2026-02-20", -40, cost=2000),   # 2 days after the loss → post-loss
          trip("L", "2026-02-01", "2026-02-25", -20),              # same day as the loss closed, same symbol
          trip("F", "2026-02-10", "2026-03-01", 80)]               # 9 days later → baseline
    pl = behavior.post_loss(ts)
    syms = sorted(t["symbol"] for t in pl["trips"])
    assert syms == ["L", "R"]
    assert pl["same_symbol"] == 1
    assert pl["after"]["n"] == 2 and pl["base"]["n"] == 2
    assert pl["after"]["exp"] == pytest.approx(-30)
    assert pl["median_cost_after"] == pytest.approx(1500)


def test_split_by_avg_down():
    ts = [trip("A", "2026-01-01", "2026-02-01", -100, avg_down=2), trip("B", "2026-01-01", "2026-02-01", 50),
          trip("C", "2026-01-01", "2026-02-01", 70, add_up=1)]
    s = behavior.split_by(ts, "avg_down")
    assert s["yes"]["n"] == 1 and s["no"]["n"] == 2 and s["events"] == 2
    assert s["yes"]["exp"] == pytest.approx(-100) and s["no"]["exp"] == pytest.approx(60)


def test_after_exit_drift_and_skips():
    days = [f"2026-01-{d:02d}" for d in range(1, 31)] + [f"2026-02-{d:02d}" for d in range(1, 29)]
    up = Series("UP", [(d, 10.0 + i * 0.1) for i, d in enumerate(days)])      # keeps rising after exit
    ts = [trip("UP", "2025-12-01", "2026-01-05", 100, exit_px=10.4),
          trip("UP", "2025-12-01", "2026-02-20", 100, exit_px=15.0),          # not enough later data
          trip("NOPX", "2025-12-01", "2026-01-05", -10)]
    ae = behavior.after_exit(ts, {"UP": up}, horizons=(20, 60))
    g = ae["groups"]["win"]
    assert g[20]["n"] == 1
    assert g[20]["median"] == pytest.approx((10.0 + 24 * 0.1) / 10.4 * 100 - 100)
    assert g[60]["n"] == 0
    assert ae["skipped"][20] == 2 and ae["skipped"][60] == 3
    assert ae["groups"]["loss"][20]["n"] == 0


def test_after_exit_rejects_price_basis_mismatch():
    s = Series("S", [("2026-01-01", 100.0), ("2026-01-02", 101.0)])
    ae = behavior.after_exit([trip("S", "2025-12-01", "2026-01-01", 10, exit_px=10.0)], {"S": s}, horizons=(1,))
    assert ae["groups"]["win"][1]["n"] == 0 and ae["skipped"][1] == 1


def test_sizing_single_and_scaled():
    total = [{"day": "2026-01-01", "value": 1000.0}, {"day": "2026-01-02", "value": 2000.0}, {"day": "2026-01-03", "value": 4000.0}]
    ts = [trip("A", "2026-01-01", "2026-01-03", 10, cost=300),                       # 300/1000 = 30% → flagged
          trip("B", "2026-01-02", "2026-01-03", -10, cost=500, buys=3, max_cost=800)]  # 800/max(2000,4000) = 20%
    sz = behavior.sizing(ts, total)
    by = {r["symbol"]: r for r in sz["flagged"]}
    assert set(by) == {"A"} and by["A"]["size_pct"] == pytest.approx(30)
    assert sz["_sizes"][id(ts[1])] == pytest.approx(20)
    assert sz["big"]["n"] == 1 and sz["rest"]["n"] == 1


def test_monthly_busy_and_by_symbol():
    ts = [trip("A", "2026-01-01", f"2026-01-{d:02d}", -10) for d in range(2, 8)]   # 6 trips in Jan
    ts += [trip("B", "2026-01-01", "2026-02-10", 50), trip("C", "2026-01-01", "2026-03-10", 40),
           trip("C", "2026-01-01", "2026-04-10", 40)]
    m = behavior.monthly(ts)
    assert m["median_n"] == 1 and m["threshold"] == 4
    assert m["busy_months"] == ["2026-01"]
    assert m["busy"]["n"] == 6 and m["calm"]["n"] == 3
    bs = behavior.by_symbol(ts)
    assert bs["top"][0]["symbol"] == "C" and bs["top"][0]["realized"] == pytest.approx(80)
    assert bs["bottom"][0]["symbol"] == "A" and bs["bottom"][0]["n"] == 6
    assert [x["symbol"] for x in bs["repeat"]] == ["A"]


def test_analyze_findings_summary_and_brief():
    ts = []
    for i in range(25):  # losers held long, winners short; post-loss trips lose
        ts.append(trip(f"W{i}", "2025-01-01", "2025-01-11", 100))
        ts.append(trip(f"L{i}", "2025-01-01", "2025-06-01", -60, avg_down=1))
    r = behavior.analyze(ts, open_pos=[], total=[{"day": "2025-01-01", "value": 100000.0}], series={}, as_of="2025-06-02")
    keys = {f["key"]: f for f in r["findings"]}
    assert keys["disposition"]["tone"] == "warn"
    assert keys["avg_down"]["tone"] == "warn"
    for f in r["findings"]:
        assert "n" in f and f["evidence"] and f["question"]
        for bad in ("sat ", "al ", "satmalısın", "almalısın"):
            assert bad not in f["question"].lower()
    assert "50 kapanan işlem" in r["summary"]
    b = behavior.brief(r)
    assert b["trips"] == 50 and b["hit_pct"] == 50.0 and b["findings"]
    import json
    json.dumps(b)
    assert behavior.analyze([], as_of="2025-01-01")["empty"] is True
    assert behavior.brief({"empty": True}) is None
