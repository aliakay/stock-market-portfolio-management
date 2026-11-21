import pytest

from borsa_radar import alerts, risk, thesis
from borsa_radar.util import tr_n


def test_parse_rules():
    assert thesis.parse_rule("stop 95") == ("price_below", "95", None)
    assert thesis.parse_rule("hedef 250") == ("price_above", "250", None)
    assert thesis.parse_rule("zarar %30") == ("loss_pct", "30", None)
    assert thesis.parse_rule("karne 40")[0] == "karne_below"
    assert thesis.parse_rule("gelir 20")[0] == "rev_growth_below"
    assert thesis.parse_rule("sulandırma 5")[0] == "dilution_above"
    assert thesis.parse_rule("tarih 2027-03-31 ürün çıkmazsa") == ("deadline", "2027-03-31", "ürün çıkmazsa")
    assert thesis.parse_rule("CEO ayrılırsa") == ("manual", "CEO ayrılırsa", None)
    with pytest.raises(ValueError):
        thesis.parse_rule("tarih yarın")


def test_rule_labels_turkish_numbers():
    assert thesis.rule_label({"kind": "price_below", "param": "95"}) == "Kapanış 95$ altına inerse"
    assert thesis.rule_label({"kind": "price_above", "param": "1250.5"}) == "Kapanış 1.250,50$ üstüne çıkarsa (hedef)"


def test_rule_eval_with_fake_context():
    t = {"symbol": "X"}
    ctx = {"close": lambda s: ("2026-11-13", 90.0), "pos": {"X": {"unreal_pct": -35.0}}, "regime": {"overall": 30, "day": "2026-11-13"}}
    assert thesis._eval({"kind": "price_below", "param": "95"}, t, ctx)[0] == "triggered"
    assert thesis._eval({"kind": "price_above", "param": "95"}, t, ctx)[0] == "ok"
    assert thesis._eval({"kind": "loss_pct", "param": "30"}, t, ctx)[0] == "triggered"
    assert thesis._eval({"kind": "regime_below", "param": "40"}, t, ctx)[0] == "triggered"
    assert thesis._eval({"kind": "deadline", "param": "2000-01-01"}, t, ctx)[0] == "triggered"
    assert thesis._eval({"kind": "manual", "param": "x"}, t, ctx)[0] == "unknown"


def test_alert_parse_and_label():
    assert alerts.parse("NVDA 150 altı") == {"symbol": "NVDA", "cond": "below", "level": 150.0}
    assert alerts.parse("nvda kapanış 150,5 altı") == {"symbol": "NVDA", "cond": "close_below", "level": 150.5}
    assert alerts.parse("MU > 900")["cond"] == "above"
    assert alerts.parse("EOSE %8 hareket") == {"symbol": "EOSE", "cond": "move", "level": 8.0}
    assert alerts.label({"symbol": "NVDA", "cond": "close_below", "level": 150.0}) == "NVDA 150$ altında kapanırsa"
    with pytest.raises(ValueError):
        alerts.parse("NVDA 150")


def test_regime_ceiling_and_returns():
    assert risk.ceiling(75) == 100 and risk.ceiling(64) == 80 and risk.ceiling(10) == 25 and risk.ceiling(None) is None
    from borsa_radar.prices import Series
    s = Series("X", [("2026-01-02", 100.0), ("2026-01-05", 110.0), ("2026-01-06", 99.0)])
    r = risk._returns(s, ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"])
    assert r[0] is None and r[1] is None and r[2] == pytest.approx(0.1) and r[3] == pytest.approx(-0.1)
    beta, n = risk._beta([0.02 * (i % 3 - 1) for i in range(60)], [0.01 * (i % 3 - 1) for i in range(60)])
    assert beta == pytest.approx(2.0) and n == 60


def test_tr_n():
    assert tr_n(1234.5, 2) == "1.234,50" and tr_n(-3.06, 2, True) == "-3,06" and tr_n(None) == "—"
