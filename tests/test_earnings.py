import pytest

from borsa_radar import earnings as E


def bar(day, o, c):
    return {"day": day, "open": o, "high": max(o, c), "low": min(o, c), "close": c}


BARS = [bar("2026-11-10", 100, 100), bar("2026-11-11", 100, 102), bar("2026-11-12", 102, 104),
        bar("2026-11-13", 110, 112), bar("2026-11-16", 112, 109)]


def test_timing_from_yahoo_timestamps():
    assert E.timing_from_dt("2026-11-17T16:00:00-05:00") == "amc"
    assert E.timing_from_dt("2026-11-12T06:30:00-05:00") == "bmo"
    assert E.timing_from_dt("2026-11-12T12:00:00-05:00") is None
    assert E.timing_from_dt("2026-11-12T00:00:00-05:00") is None
    assert E.timing_from_name("NVDA bilanço (kapanış sonrası, EPS beklentisi 2.52)") == "amc"
    assert E.timing_from_name("EOSE bilanço (açılış öncesi)") == "bmo"
    assert E.timing_from_name("X bilanço (saat belirsiz)") is None


def test_reaction_day_skips_weekend():
    assert E.reaction_day("2026-11-13", "amc") == "2026-11-16"     # Friday after close → Monday
    assert E.reaction_day("2026-11-12", "bmo") == "2026-11-12"
    assert E.reaction_day("2026-11-12", None) == "2026-11-13"


def test_reaction_after_close_uses_report_day_close_and_next_session():
    r = E.reaction(BARS, "2026-11-12", "amc")
    assert r["pre_day"] == "2026-11-12" and r["day"] == "2026-11-13"
    assert r["move_pct"] == pytest.approx((112 / 104 - 1) * 100)
    assert r["gap_pct"] == pytest.approx((110 / 104 - 1) * 100)
    assert r["next_day"] == "2026-11-16" and r["next_pct"] == pytest.approx((109 / 112 - 1) * 100)


def test_reaction_before_open_uses_previous_close_and_same_day():
    r = E.reaction(BARS, "2026-11-13", "bmo")
    assert r["pre_day"] == "2026-11-12" and r["day"] == "2026-11-13"
    assert r["move_pct"] == pytest.approx((112 / 104 - 1) * 100)


def test_reaction_unknown_timing_spans_two_sessions():
    r = E.reaction(BARS, "2026-11-12", None)
    assert r["pre_day"] == "2026-11-11" and r["day"] == "2026-11-13"
    assert r["window"] == "2 seans"


def test_reaction_missing_when_no_session_after():
    assert E.reaction(BARS, "2026-11-16", "amc") is None
    assert E.reaction([], "2026-11-12", "amc") is None


def test_history_stats_counts_beats_and_moves():
    rows = [
        {"date": "2026-08-26", "eps_est": 2.09, "eps_act": 2.22, "surprise": 6.2, "reaction": {"move_pct": 8.0}},
        {"date": "2026-05-20", "eps_est": 1.77, "eps_act": 1.87, "surprise": 5.5, "reaction": {"move_pct": -2.0}},
        {"date": "2026-02-25", "eps_est": 0.12, "eps_act": 0.12, "surprise": -4.2, "reaction": {"move_pct": -5.0}},  # rounded EPS, surprise decides
        {"date": "2025-11-19", "eps_est": None, "eps_act": None, "surprise": None, "reaction": None},
    ]
    s = E.history_stats(rows)
    assert s["n_moves"] == 3 and s["up"] == 1 and s["down"] == 2
    assert s["avg_abs"] == pytest.approx(5.0)
    assert (s["beats"], s["misses"], s["eps_n"]) == (2, 1, 3)
    assert s["max"] == 8.0 and s["max_day"] == "2026-08-26"
    assert s["beat_up"] == 1 and s["beat_n"] == 2


def chain(spot=100.0):
    def leg(k, bid, ask, iv=0.5):
        return {"strike": k, "bid": bid, "ask": ask, "last": (bid + ask) / 2, "iv": iv, "oi": 10, "vol": 5}
    return {"spot": spot, "expiries": [
        {"expiry": "2026-11-14", "calls": [leg(100, 1, 1.2)], "puts": [leg(100, 1, 1.2)]},             # before the report
        {"expiry": "2026-11-18", "calls": [leg(95, 6, 6.4), leg(100, 3.9, 4.1)], "puts": [leg(100, 3.8, 4.2), leg(105, 7, 7.4)]},
        {"expiry": "2026-12-18", "calls": [leg(100, 9, 9.4)], "puts": [leg(100, 9, 9.4)]},
    ]}


def test_implied_move_takes_first_expiry_after_report():
    im = E.implied_move(chain(), "2026-11-18")
    assert im["expiry"] == "2026-11-18" and im["strike"] == 100
    assert im["move"] == pytest.approx(8.0) and im["pct"] == pytest.approx(8.0)
    assert not im["diluted"] and "ortası" in im["method"]
    far = E.implied_move(chain(), "2026-11-20")
    assert far["expiry"] == "2026-12-18" and far["diluted"]
    assert E.implied_move(chain(), "2027-01-10") is None
    assert E.implied_move(None, "2026-11-18") is None


def test_revision_pct_and_dollar_for_loss_makers():
    up = E.revision([{"period": "0q", "current": 2.5, "7daysAgo": 2.45, "30daysAgo": 2.4, "90daysAgo": 2.0}])
    assert up["direction"] == "yukarı" and up["chg30"] == pytest.approx((2.5 / 2.4 - 1) * 100)
    neg = E.revision([{"period": "0q", "current": 0.08, "7daysAgo": -0.23, "30daysAgo": -0.23, "90daysAgo": -0.23}])
    assert neg["chg7"] is None and neg["diff7"] == pytest.approx(0.31)
    assert neg["direction"] == "yukarı"
    assert E.rev_text(neg, 7) == "+0,31$"
    assert E.rev_text(up, 30) == "%+4,2"
    assert E.revision([]) is None


def test_exposure_dollars_at_risk():
    pos = [{"symbol": "NVDA", "asset": "stock", "qty": 10, "value": 2000.0, "weight": 20.0, "cost": 1500.0, "unreal_pct": 33.3,
            "price": 200.0, "price_day": "2026-11-13", "sub": "P1"},
           {"symbol": "NVDA261218C00200000", "asset": "option", "qty": 1, "value": 500.0, "weight": 5.0, "cost": 400.0,
            "unreal_pct": 25.0, "price": 5.0, "price_day": "2026-11-13", "sub": "P1"},
           {"symbol": "MU", "asset": "stock", "qty": 1, "value": 100.0, "weight": 1.0, "cost": 90.0, "unreal_pct": 11.0,
            "price": 100.0, "price_day": "2026-11-13", "sub": "P1"}]
    ex = E.exposure("NVDA", 7.5, 4.0, pos)
    assert ex["held"] and ex["qty"] == 10 and ex["value"] == 2000.0 and ex["avg"] == 150.0
    assert ex["risk_implied"] == pytest.approx(150.0) and ex["risk_hist"] == pytest.approx(80.0)
    assert ex["options"][0]["type"] == "CALL" and ex["options"][0]["strike"] == 200.0
    none = E.exposure("AAPL", 7.5, 4.0, pos)
    assert not none["held"] and none["risk_implied"] is None


def test_day_tr():
    assert E.day_tr("2026-11-17") == "17.11 Sal"
    assert E.day_tr("2026-11-17", short=False) == "17.11 Salı"
    assert E.day_tr(None) == "—"
