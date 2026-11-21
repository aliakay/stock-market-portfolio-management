import math

import pytest

from borsa_radar import hub, options, scorecard, stockdata, technicals


def F(data):
    return stockdata.Fetched(data, "2026-11-15T12:00:00+00:00", "test")


def test_indicators():
    xs = [float(i) for i in range(1, 61)]
    assert technicals.sma(xs, 10) == pytest.approx(55.5)
    assert technicals.rsi(xs) == 100.0
    assert technicals.ret(xs, 10) == pytest.approx((60 / 50 - 1) * 100)
    assert technicals.max_drawdown([10, 12, 6, 13]) == pytest.approx(-50)
    flat = [100.0] * 30
    assert technicals.hv(flat) == 0
    zig = [100.0 + (1 if i % 2 else -1) for i in range(40)]
    assert 40 < technicals.rsi(zig) < 60


def test_trend_score_bounds():
    s = dict(above20=True, above50=True, above200=True, golden=True, chg_63d=40, rs63_vs_spy=40, rsi14=60)
    assert technicals.trend_score(s) == 100
    s.update(above20=False, above50=False, above200=False, golden=False, chg_63d=-40, rs63_vs_spy=-40, rsi14=15)
    assert technicals.trend_score(s) == 0
    assert technicals.trend_label(s) == "Düşüş trendi"


def test_max_pain_and_gamma():
    calls = [{"strike": k, "oi": oi} for k, oi in ((90, 10), (100, 100), (110, 500))]
    puts = [{"strike": k, "oi": oi} for k, oi in ((90, 500), (100, 100), (110, 10))]
    assert options.max_pain(calls, puts) == 100
    assert options.bs_gamma(100, 100, 0.1, 0.3) > options.bs_gamma(100, 130, 0.1, 0.3) > 0
    assert options.bs_gamma(100, 100, 0, 0.3) == 0


def test_options_score_direction():
    base = dict(pc_oi=0.5, pc_vol=0.5, gex=1.0, gamma_sign="pozitif", put_wall=90, call_wall=110, spot=100, unusual=[], atm_iv=0.3)
    bull, _ = options.score(base, 0.3)
    bear, _ = options.score(base | dict(pc_oi=1.6, pc_vol=1.8, gex=-1.0, gamma_sign="negatif"), 0.3)
    assert bull > 60 > bear
    assert options.score(None, None) == (None, [])


def test_formatting():
    assert hub.num(1234.567) == "1.234,57"
    assert hub.pct(12.34) == "%+12,3"
    assert hub.pct(None) == "—"
    assert hub.big(2.88e12) == "2,88T$"


def test_two_quarter_eps_matches_reference():
    # values validated against the reference screenshot: 4.69 now, 4.80 30 days ago
    trend = [{"period": "0q", "current": 2.39876, "30daysAgo": 2.43886},
             {"period": "+1q", "current": 2.29664, "30daysAgo": 2.35766}]
    assert hub.two_q_eps(trend) == pytest.approx(4.6954, abs=1e-3)
    assert hub.two_q_eps(trend, "30daysAgo") == pytest.approx(4.7965, abs=1e-3)
    assert 266.66 / (hub.two_q_eps(trend) * 2) == pytest.approx(28.4, abs=0.05)


def test_fair_value_median_and_confidence():
    x = hub.Inputs(
        ptc=F({"targetMedian": 120}),
        y_earnings_estimate=F([{"period": "0y", "avg": 4.0}, {"period": "+1y", "avg": 5.0, "growth": 0.1}]),
        y_growth_estimates=F([{"period": "LTG", "stockTrend": 0.10}]),
        ratios_a=F([{"priceToEarningsRatio": p} for p in (22, 24, 26, -5, 300)]),
    )
    fv = scorecard.fair_value(x, 100)
    vals = sorted(m["value"] for m in fv["methods"])
    assert vals == pytest.approx([110.0, 120.0, 120.0])  # 5×(7+1.5×10), target, 5×median(22,24,26)
    assert fv["fair"] == pytest.approx(120)
    assert fv["confidence"] == "yüksek"
    assert scorecard.fair_value(hub.Inputs(), 100)["fair"] is None


def test_pe_cap():
    x = hub.Inputs(y_earnings_estimate=F([{"period": "+1y", "avg": 10.0}]),
                   ratios_a=F([{"priceToEarningsRatio": p} for p in (60, 70, 80)]))
    fv = scorecard.fair_value(x, 100)
    assert fv["methods"][0]["value"] == pytest.approx(10 * scorecard.PE_CAP)
    assert fv["confidence"] == "düşük"  # single method


def test_weighting_and_bands():
    dims = [{"key": "a", "score": 80}, {"key": "b", "score": None}, {"key": "c", "score": 40}]
    assert scorecard.weighted(dims, {"a": 30, "b": 50, "c": 10}) == pytest.approx(70)
    assert scorecard.band(67)[0] == "pos" and scorecard.band(66.9)[0] == "warn" and scorecard.band(44)[0] == "neg"
    assert scorecard.band(None)[1] == "Yetersiz veri"


def test_summary_number_guard(monkeypatch):
    card = {"symbol": "XYZ", "overall": 72.4, "label": "Güçlü", "tone": "pos",
            "dims": [{"key": "temel", "name": "Temel", "score": 81.0, "parts": [{"label": "Net marj", "value": "%16,9", "pts": 72}]}],
            "fair": {"fair": 120.0, "upside": 20.0, "confidence": "orta"}}
    monkeypatch.setattr(scorecard.llm, "complete", lambda *a, **k: ("XYZ 72 puanla güçlü; net marj %16,9. Yani temel sağlam.", "m"))
    text, model = scorecard.llm_summary(card)
    assert model == "m" and "16,9" in text
    monkeypatch.setattr(scorecard.llm, "complete", lambda *a, **k: ("XYZ hedefi 175 dolar.", "m"))
    text, model = scorecard.llm_summary(card)
    assert model.startswith("şablon") and "175" not in text
