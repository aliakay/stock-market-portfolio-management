import math

import pandas as pd
import pytest

from borsa_radar import breadth, pulse, regime, rotation


def test_lin_and_clamp():
    assert regime.lin(5, 0, 0, 10, 100) == 50
    assert regime.lin(-5, 0, 0, 10, 100) == 0
    assert regime.lin(50, 12, 95, 32, 5) == 5  # descending map clamps to the low end


def test_trend_rule_extremes():
    up, _ = regime.score_trend(110, 105, 100, 95)
    down, _ = regime.score_trend(80, 90, 95, 100)
    assert up > 85 and down < 10


def test_vol_rule_term_structure():
    calm, _ = regime.score_vol(13, 16)
    stressed, why = regime.score_vol(25, 24)
    assert calm > 85 and stressed < 30
    assert any("ters vade" in w for w in why)


def test_breadth_rule_and_fallback():
    strong, _ = regime.score_breadth(75, 75, 30)
    weak, _ = regime.score_breadth(30, 25, -30)
    assert strong == 100 and weak == 0
    fb, why = regime.score_breadth(None, None, None, sector_pct=90)
    assert fb == 90 and "yedek" in why[0]
    assert regime.score_breadth(None, None, None) == (None, [])


def test_other_rules():
    assert regime.score_ratio_trend(1.1, 1.0, 3) == 87
    assert regime.score_ratio_trend(0.9, 1.0, -3) == 13
    inv, why = regime.score_curve(-0.5, 50)
    assert inv < 25 and any("şoku" in w for w in why)
    assert regime.score_dollar(3)[0] == 25 and regime.score_dollar(-3)[0] == 75
    assert regime.score_particip(4, 6)[0] == 90


def test_combine_renormalizes_and_labels():
    assert regime.combine({"trend": 80, "vol": None, "breadth": 40}, {"trend": 25, "vol": 20, "breadth": 25}) == 60
    assert regime.label_for(75, True, 15) == ("Risk-on Boğa", "pos")
    assert regime.label_for(75, False, 15)[0] == "Nötr-Dağınık"  # no bull label below the 200-day
    assert regime.label_for(62, True, 31)[0] == "Risk-off"  # panic VIX caps the label
    assert regime.label_for(20, False, 40)[0] == "Ayı"
    assert regime.label_for(None, True, None)[0] == "doğrulanamadı"


def test_weather_points_range():
    best = regime.weather_points(4, -20, 900, 0.8)
    worst = regime.weather_points(-4, 20, -900, 1.1)
    assert sum(p for _, p in best) == 8 and sum(p for _, p in worst) == -8
    assert len(regime.weather_points(0, 0, None, None)) == 2


def test_rrg_quadrants():
    assert rotation.quadrant(101, 101) == "Lider"
    assert rotation.quadrant(101, 99) == "Zayıflayan"
    assert rotation.quadrant(99, 99) == "Gecikmeli"
    assert rotation.quadrant(99, 101) == "Toparlanan"
    # steadily outperforming ETF: RS rising → ratio > 100 and momentum ≥ ~100
    rs = pd.Series([1 + 0.01 * k for k in range(30)])
    r = rotation.rrg(rs)
    assert r["ratio"].iloc[-1] > 100
    assert rotation.quadrant(r["ratio"].iloc[-1], r["mom"].iloc[-1]) in ("Lider", "Zayıflayan")
    # sharply reversing laggard → Toparlanan
    rs2 = pd.Series([2 - 0.03 * k for k in range(25)] + [1.29 + 0.015 * k for k in range(3)])
    r2 = rotation.rrg(rs2)
    assert rotation.quadrant(r2["ratio"].iloc[-1], r2["mom"].iloc[-1]) == "Toparlanan"


def test_weekly_sampling_and_band():
    days = pd.bdate_range("2026-01-05", periods=15).strftime("%Y-%m-%d")
    s = pd.Series(range(15), index=days)
    w = rotation.weekly(s)
    assert list(w.values) == [4, 9, 14]
    assert rotation.band_pos(pd.Series([1, 2, 3, 4, 5])) == 100


def test_breadth_frame():
    days = [f"2026-01-{d:02d}" for d in range(1, 31)] + [f"2026-02-{d:02d}" for d in range(1, 29)] \
        + [f"2026-03-{d:02d}" for d in range(1, 32)] + [f"2026-04-{d:02d}" for d in range(1, 31)] \
        + [f"2026-05-{d:02d}" for d in range(1, 32)] + [f"2026-06-{d:02d}" for d in range(1, 31)] \
        + [f"2026-07-{d:02d}" for d in range(1, 32)] + [f"2026-08-{d:02d}" for d in range(1, 32)]
    n = len(days)
    up = [100 + k for k in range(n)]          # always rising: above both averages, new highs
    down = [500 - k for k in range(n)]        # always falling: below both, new lows
    flat = [100.0] * n                        # unchanged: neither
    fr = breadth.compute_frame(pd.DataFrame({"UP": up, "DN": down, "FL": flat}, index=days))
    last = fr.iloc[-1]
    assert last["n"] == 3
    assert last["above50"] == pytest.approx(100 / 3) and last["above200"] == pytest.approx(100 / 3)
    assert (last["adv"], last["dec"]) == (1, 1)
    assert (last["nh"], last["nl"]) == (2, 2)  # flat series is both its own max and min
    assert fr["ad_line"].iloc[-1] == 0
    assert math.isnan(fr["above200"].iloc[100])  # not enough history yet


def test_pulse_helpers():
    assert pulse.third_friday(2026, 11).isoformat() == "2026-11-20"
    o = pulse.next_opex(pulse.date(2026, 11, 14))
    assert o["date"] == "2026-11-20" and not o["triple"] and o["t_minus"] == 5
    assert pulse.next_opex(pulse.date(2026, 11, 21))["date"] == "2026-12-18"
    assert pulse.stage(110, 100, 98, 95) == (2, "Yükseliş")
    assert pulse.stage(90, 100, 102, 105) == (4, "Düşüş")
    close = pd.Series([100, 99, 98.5, 99, 97] * 6, dtype=float)
    vol = pd.Series([1, 2, 3, 1, 5] * 6, dtype=float)
    assert pulse.distribution_days(close, vol) == 15  # 3 heavier down days per 5-day cycle


def test_log_redaction(caplog):
    import logging
    from borsa_radar.util import RedactFilter, redact
    url = "https://x.test/grades?symbol=MU&apikey=SECRET1&a=1 and ?api_token=SECRET2&token=SECRET3 registrationkey=SECRET4"
    out = redact(url)
    assert "SECRET" not in out and "symbol=MU" in out and "a=1" in out
    rec = logging.LogRecord("t", logging.WARNING, __file__, 1, "failed: %s", (url,), None)
    RedactFilter().filter(rec)
    assert "SECRET" not in rec.getMessage()
    try:
        raise RuntimeError(url)
    except RuntimeError:
        import sys
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    RedactFilter().filter(rec)
    assert "SECRET" not in rec.exc_text
