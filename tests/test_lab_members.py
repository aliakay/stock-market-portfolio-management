"""Point-in-time S&P 500 membership: intervals, ticker matching (recycled tickers, share classes, renames), the lab's
member mask, and the delisting range of the idea backtest."""

import csv

import numpy as np
import pytest

from borsa_radar import ideas, lab, lab_data


def test_membership_intervals_open_close_and_since():
    rows = [("2004-01-02", ["A", "B"]), ("2005-06-01", ["A", "C"]), ("2008-03-03", ["C", "D"]), ("2010-01-04", ["C", "D", "A"])]
    iv = lab_data.membership_intervals(rows, since="2006-01-01")
    assert "B" not in iv                                   # left in 2005, before `since`
    assert iv["A"] == [["2004-01-02", "2008-03-03"], ["2010-01-04", None]]
    assert iv["C"] == [["2005-06-01", None]] and iv["D"] == [["2008-03-03", None]]


def _row(t, name, dead, first, last, rel=""):
    return {"ticker": t, "name": name, "isdelisted": "Y" if dead else "N", "firstpricedate": first, "lastpricedate": last,
            "relatedtickers": rel}


def test_resolve_recycled_ticker_chain_and_share_class():
    rows = [
        _row("CEG1", "OLD CONSTELLATION", True, "1998-01-02", "2012-03-09", "CEG"),   # old company, recycled ticker
        _row("CEG", "CONSTELLATION ENERGY", False, "2022-01-19", "2026-11-17"),
        _row("TFCF", "21ST CENTURY FOX", True, "1998-01-02", "2019-03-20", "FOXA FOX"),  # FOXA chain
        _row("FOXA", "FOX CORP", False, "2019-03-13", "2026-11-17", "FOX"),
        _row("FOX", "FOX CORP", False, "2019-03-13", "2026-11-17", "FOXA"),
        _row("BRK.A", "BERKSHIRE", False, "1998-01-02", "2026-11-17", "BRK.B"),
        _row("BRK.B", "BERKSHIRE", False, "1998-01-02", "2026-11-17", "BRK.A"),
    ]
    iv = {"CEG": [["2006-01-01", "2012-03-12"], ["2022-02-02", None]], "FOXA": [["2006-01-01", None]], "BRK.B": [["2010-02-16", None]]}
    res = lab_data.resolve(iv, rows, have={"BRK-B", "CEG"})
    ceg = res["CEG"]
    assert [(r["status"], r["sym"]) for r in ceg] == [("delisted", None), ("in_hist", "CEG")]
    assert ceg[0]["last"] == "2012-03-09" and ceg[1]["iv"] == ["2022-02-02", None]
    fox = res["FOXA"]
    assert [(r["status"], r["sym"]) for r in fox] == [("delisted", None), ("listed", "FOXA")]   # exact class, not FOX
    assert fox[1]["iv"][0] == "2019-03-21"                  # 21st Century Fox keeps the period while it still trades
    assert [(r["status"], r["sym"]) for r in res["BRK.B"]] == [("in_hist", "BRK-B")]          # '.' → '-' for Yahoo


def test_resolve_without_ticker_table_falls_back_to_names():
    res = lab_data.resolve({"BRK.B": [["2010-02-16", None]], "XYZ": [["2007-01-02", "2009-01-02"]]}, [], have={"BRK-B"})
    assert res["BRK.B"][0]["status"] == "in_hist" and res["XYZ"][0]["status"] == "unmatched"


def test_merge_intervals():
    assert lab_data._merge([["2010-01-01", "2012-01-01"], ["2011-06-01", "2013-01-01"], ["2015-01-01", None], ["2016-01-01", "2017-01-01"]]) \
        == [["2010-01-01", "2013-01-01"], ["2015-01-01", None]]


def test_member_mask_limits_signals_but_not_exits():
    days = np.arange(20200101, 20200111)
    m = lab.member_mask(days, [[20200103, 20200106], [20200109, lab_data.OPEN_END]])
    assert list(np.flatnonzero(m)) == [2, 3, 4, 8, 9]
    # a dip at bar 251 signals; outside membership it does not
    c = list(np.linspace(50, 100, 250)) + [95, 90, 99, 101, 102]
    bars = np.column_stack([np.arange(20200101, 20200101 + len(c), dtype=float), c, c, c, c, np.full(len(c), 1e7)])
    x = lab.Ind(bars)
    s = lab.Spec("t", "rsi2", 10, "sma5")
    assert lab.simulate(s, "X", x, None)
    no = np.ones(x.n, dtype=bool)
    no[245:] = False                                       # left the index before the dip
    assert lab.simulate(s, "X", x, None, member=no) == []


def test_build_members_counts_the_gap(tmp_path, monkeypatch):
    hist = tmp_path / "h.csv"
    hist.write_text('date,tickers\n2005-01-03,"AAA,BBB,OLD"\n2008-06-02,"AAA,BBB,NEWCO"\n2012-01-03,"AAA,NEWCO,CCC"\n')
    tk = tmp_path / "t.csv"
    with tk.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=lab_data._TICKER_COLS, extrasaction="ignore")
        w.writeheader()
        for r in (_row("AAA", "AAA INC", False, "1998-01-02", "2026-11-17"), _row("BBB", "BBB INC", False, "1998-01-02", "2026-11-17"),
                  _row("OLDQ", "OLD CO", True, "1998-01-02", "2008-05-30", "OLD"), _row("NEWCO", "NEWCO", False, "2007-01-02", "2026-11-17"),
                  _row("CCC", "CCC", False, "2011-01-03", "2026-11-17")):
            w.writerow(r)
    monkeypatch.setattr(lab_data, "SP500_HIST_PATH", hist)
    monkeypatch.setattr(lab_data, "TICKERS_PATH", tk)
    monkeypatch.setattr(lab_data, "MEMBERS_PATH", tmp_path / "m.json")
    monkeypatch.setattr(lab_data, "HIST_PATH", tmp_path / "none.npz")
    monkeypatch.setattr(lab_data, "EX_PATH", tmp_path / "none2.npz")
    m = lab_data.build_members(refresh=False, current=["AAA", "NEWCO", "CCC", "DDD"])
    st = m["status"]
    assert st["delisted"]["names"] == 1 and m["missing"][0]["name"] == "OLD CO"
    assert st["listed"]["names"] == 4                      # nothing on file in this test
    assert m["members"]["BBB"] == [[20060101, 20120103]]
    assert m["members"]["DDD"][0][0] == 20120103           # joined after the file's last date (today's list)
    assert abs(sum(v["share_days"] for v in st.values()) - 100) < 0.5
    assert set(lab_data.members_map()) == {"AAA", "BBB", "NEWCO", "CCC", "DDD"}


def test_delisting_by_cik(tmp_path, monkeypatch):
    tk = tmp_path / "t.csv"
    with tk.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=lab_data._TICKER_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerow(_row("GONE", "GONE", True, "2001-01-02", "2020-05-29") | {"secfilings": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000012345"})
        w.writerow(_row("TWO.A", "TWO", True, "2001-01-02", "2019-01-02") | {"secfilings": "...CIK=0000777"})
        w.writerow(_row("TWO.B", "TWO", False, "2001-01-02", "2026-11-17") | {"secfilings": "...CIK=0000777"})
    monkeypatch.setattr(lab_data, "TICKERS_PATH", tk)
    d = lab_data.delisting_by_cik()
    assert d == {12345: 20200529, 777: None}               # one class still trades → not delisted


def test_delisting_range_hand_case():
    # two priced picks: +10 % and +4 % vs SPY (+6 / +2 vs the universe); one pick delisted with SPY +5 %, universe +3 %;
    # one surviving pick without prices → w = 2 / 3
    r = ideas._range([(0.10, 0.06), (0.04, 0.02)], [("2020-02-20", 0.05, 0.03)], alive=1, unknown=0)
    w = 2 / 3
    assert r["ex_spy"] == pytest.approx(7.0) and r["gone"] == 1 and r["w"] == pytest.approx(w)
    assert r["ex_spy_30"] == pytest.approx((0.14 + w * (-0.30 - 0.05)) / (2 + w) * 100)
    assert r["ex_spy_100"] == pytest.approx((0.14 + w * (-1.0 - 0.05)) / (2 + w) * 100)
    assert r["ex_uni_30"] == pytest.approx((0.08 + w * (-0.30 - 0.03)) / (2 + w) * 100)
    assert r["ex_spy_30_all"] == pytest.approx((0.14 + (-0.30 - 0.05)) / 3 * 100)
    assert ideas._range([], [], 0, 0) == {"n": 0, "gone": 0, "alive_unpriced": 0, "unknown": 0}
