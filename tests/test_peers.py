import pytest

from borsa_radar import peers


def C(v):
    return {"value": v, "source": "test", "fetched": "2026-11-15", "note": None}


def row(**kw):
    base = {k: None for k, *_ in peers.METRICS}
    base.update(kw)
    return {k: C(v) for k, v in base.items()}


def test_ratio_math():
    assert peers.ratio(10, 4) == 2.5
    assert peers.ratio(10, 0) is None
    assert peers.ratio(None, 4) is None
    assert peers.growth_pct(120, 100) == pytest.approx(20)
    assert peers.growth_pct(120, 0) is None
    assert peers.growth_pct(120, -5) is None
    assert peers.median([3, None, 1, 2]) == 2
    assert peers.median([None]) is None
    assert peers.premium(30, 20) == pytest.approx(50)
    assert peers.premium(15, 20) == pytest.approx(-25)
    assert peers.premium(15, -20) is None


def test_percentile_and_meaningful():
    assert peers.percentile_rank(25, [10, 20, 30, 40]) == 50
    assert peers.percentile_rank(20, [10, 20, 30, 40]) == pytest.approx(37.5)
    assert peers.percentile_rank(5, []) is None
    assert not peers.meaningful("pe_ttm", -12)
    assert peers.meaningful("pe_ttm", 12)
    assert peers.meaningful("net_margin", -5)      # quality metrics can be negative
    assert not peers.meaningful("ev_ebitda", float("nan"))


def test_growth_adjusted_pe():
    assert peers.growth_adjusted_pe(30, 60) == pytest.approx(0.5)
    assert peers.growth_adjusted_pe(30, -5) is None
    assert peers.growth_adjusted_pe(-30, 20) is None


def test_valuation_label_rules():
    assert peers.valuation_label({"pe_ttm": 40, "fwd_pe": 20, "ev_rev": 30})["short"] == "pahalı"
    assert peers.valuation_label({"pe_ttm": -40, "fwd_pe": -20})["short"] == "ucuz"
    assert peers.valuation_label({"pe_ttm": 10, "fwd_pe": -10, "ev_rev": 5})["short"] == "makul"
    lab = peers.valuation_label({"pe_ttm": 40, "fwd_pe": None})
    assert lab["label"] == peers.NA and lab["score"] is None
    edge = peers.valuation_label({"pe_ttm": 15, "fwd_pe": 15})
    assert edge["short"] == "pahalı" and edge["score"] == 15


def test_quality_note():
    assert "kısmen açıklanabilir" in peers.quality_note("pahalı", {"a": 5, "b": 3, "c": -1})
    assert "değer tuzağı" in peers.quality_note("ucuz", {"a": -5, "b": -3, "c": 1})
    assert peers.quality_note(None, {"a": 1}) is None


def test_analyze_target_vs_peer_median():
    table = {
        "AAA": row(market_cap=1e12, pe_ttm=60, fwd_pe=40, ev_rev=25, ev_ebitda=45, p_fcf=70,
                   rev_growth=60, gross_margin=75, op_margin=60, net_margin=50, fcf_margin=45, net_debt_ebitda=-0.3),
        "BBB": row(market_cap=3e11, pe_ttm=30, fwd_pe=25, ev_rev=10, ev_ebitda=25, p_fcf=35,
                   rev_growth=20, gross_margin=55, op_margin=30, net_margin=25, fcf_margin=25, net_debt_ebitda=0.5),
        "CCC": row(market_cap=2e11, pe_ttm=-20, fwd_pe=35, ev_rev=14, ev_ebitda=35, p_fcf=45,
                   rev_growth=10, gross_margin=60, op_margin=20, net_margin=-5, fcf_margin=15, net_debt_ebitda=1.5),
    }
    res = peers.analyze(table)
    m = res["metrics"]
    assert res["target"] == "AAA" and res["peers"] == ["BBB", "CCC"]
    assert m["pe_ttm"]["median"] == 30 and m["pe_ttm"]["n_peers"] == 1          # negative P/E excluded
    assert m["pe_ttm"]["premium_pct"] == pytest.approx(100)
    assert m["ev_rev"]["median"] == 12 and m["ev_rev"]["percentile"] == 100
    assert m["net_margin"]["diff_pts"] == pytest.approx(40)
    assert m["market_cap"]["premium_pct"] is None                                # size is not compared
    assert res["label"]["short"] == "pahalı"
    assert "kısmen açıklanabilir" in res["label"]["quality_note"]
    g = res["growth_adjusted_pe"]
    assert g["by_symbol"]["AAA"] == pytest.approx(40 / 60)
    assert g["peer_median"] == pytest.approx((25 / 20 + 35 / 10) / 2)
    assert g["target_premium_pct"] < 0                                            # cheaper per unit of growth


def test_analyze_missing_target_data_is_unverified():
    table = {"AAA": row(market_cap=1e9), "BBB": row(pe_ttm=20, fwd_pe=18)}
    res = peers.analyze(table)
    assert res["label"]["label"] == peers.NA
    assert res["metrics"]["pe_ttm"]["premium_pct"] is None


def test_yahoo_helpers():
    rows = [{"index": "Total Revenue", "2026-03-31": 10.0, "2025-12-31": 9.0, "2025-09-30": 8.0, "2025-06-30": 7.0, "2025-03-31": 5.0}]
    s = peers._yahoo_series(rows, "Total Revenue")
    assert s == [10.0, 9.0, 8.0, 7.0, 5.0]
    assert peers._ttm(s) == 34
    assert peers._ttm([1, None, 2, 3]) is None
    assert peers._yahoo_series(rows, "EBITDA") == []


def test_fmp_rev_growth_requires_same_quarter():
    q = [{"period": "Q3", "fiscalYear": "2026", "revenue": 150}, {}, {}, {},
         {"period": "Q3", "fiscalYear": "2025", "revenue": 100}]
    assert peers._fmp_rev_growth(q) == pytest.approx(50)
    q[4]["period"] = "Q2"
    assert peers._fmp_rev_growth(q) is None
    assert peers._fmp_rev_growth(q[:3]) is None


def test_flag_meaningless():
    c = {k: C(None) for k, *_ in peers.METRICS}
    c["pe_ttm"] = C(-15)
    c["ev_ebitda"] = C(-8)
    c["net_debt_ebitda"] = C(-2)
    peers._flag_meaningless(c)
    assert "anlamsız" in c["pe_ttm"]["note"]
    assert c["net_debt_ebitda"]["value"] is None and c["net_debt_ebitda"]["value_raw"] == -2


def test_static_and_universe_peers():
    group, members = peers.static_peers("sofi")
    assert group == "fintek" and "SOFI" not in members and "AFRM" in members
    assert peers.static_peers("ZZZZ") == (None, [])
    profiles = {"AMD": {"industry": "Semiconductors", "sector": "Technology"},
                "MSFT": {"industry": "Software", "sector": "Technology"},
                "XOM": {"industry": "Oil", "sector": "Energy"}}
    got = peers.universe_matches("Semiconductors", "Technology", "NVDA", ["NVDA", "MSFT", "AMD", "XOM"], profiles)
    assert [r["symbol"] for r in got] == ["AMD", "MSFT"]
    assert got[0]["strength"] == 2 and "zayıf" in got[1]["why"]


def test_signed_pct_and_rule_text():
    assert peers.signed_pct(-62.84) == "−%62,8"
    assert peers.signed_pct(5, 0) == "+%5"
    assert peers.signed_pct(None) == "—"
    assert "−%15" in peers.RULE and "+%15" in peers.RULE


def test_leverage_compared_by_gap():
    table = {"AAA": row(net_debt_ebitda=0.1), "BBB": row(net_debt_ebitda=-0.6), "CCC": row(net_debt_ebitda=0.7)}
    m = peers.analyze(table)["metrics"]["net_debt_ebitda"]
    assert m["premium_pct"] is None and m["diff_pts"] == pytest.approx(0.1 - 0.05)
