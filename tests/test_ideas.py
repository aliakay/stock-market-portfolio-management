"""Idea screener: SEC frames panel helpers, the four rules, publication lag, verdicts, page."""

from datetime import date

import numpy as np
import pytest

from borsa_radar import frames, ideas


def test_qidx_fiscal_quarters():
    assert frames._qidx("2025-12-31") == (2025, 4)
    assert frames._qidx("2026-01-25") == (2025, 4)     # NVIDIA-style fiscal Q4
    assert frames._qidx("2026-01-03") == (2025, 4)     # 52/53-week year
    assert frames._qidx("2025-09-27") == (2025, 3)
    assert frames._qidx("2026-09-03") == (2026, 3)     # Micron-style fiscal Q4


def _panel(rows: dict[str, dict[str, list]], q: list[str]) -> frames.Panel:
    """rows: ticker -> item -> values per quarter (None = NaN)."""
    items = list(frames.FLOW) + list(frames.INSTANT)
    data = {it: np.full((len(rows), len(q)), np.nan) for it in items}
    for i, (_, d) in enumerate(rows.items()):
        for it, vals in d.items():
            data[it][i] = [np.nan if v is None else v for v in vals]
    p = frames.Panel(np.arange(1, len(rows) + 1), q, data, {k + 1: (t, t + " Inc") for k, t in enumerate(rows)})
    p.derived = np.zeros((len(rows), len(q)), dtype=bool)
    return p


Q = [f"CY{y}Q{n}" for y in (2024, 2025, 2026) for n in (1, 2, 3, 4)][:10]   # CY2024Q1 … CY2026Q2


def _series(base, step, n=10):
    return [base + step * k for k in range(n)]


def test_rules_each_screen():
    m6 = 1e6
    rows = {
        # losses narrowing two quarters in a row, smaller than a year ago, revenue +~30 %
        "NARR": {"net_income": [-50 * m6, -48 * m6, -47 * m6, -46 * m6, -45 * m6, -44 * m6, -40 * m6, -35 * m6, -25 * m6, -12 * m6],
                 "revenue": _series(20 * m6, 2 * m6), "shares": [100.0] * 10},
        # three losses then a profit
        "FIRST": {"net_income": [-5 * m6] * 9 + [3 * m6], "revenue": _series(30 * m6, 2 * m6), "shares": [100.0] * 10},
        # profitable, NI +50 % on the year, revenue growth accelerating
        "ACCEL": {"net_income": [10 * m6] * 6 + [11 * m6, 12 * m6, 13 * m6, 15 * m6],
                  "revenue": [100 * m6] * 5 + [100 * m6, 101 * m6, 105 * m6, 112 * m6, 125 * m6], "shares": [100.0] * 10},
        # tiny revenue: filtered out everywhere
        "TINY": {"net_income": [-5.0] * 9 + [3.0], "revenue": _series(1.0, 0.1), "shares": [100.0] * 10},
    }
    p = _panel(rows, Q)
    aj = np.full(len(rows), len(Q) - 1)
    r = ideas.rules(ideas.metrics(p, aj))
    names = list(rows)
    hits = {code: {names[i] for i in np.flatnonzero(mk)} for code, mk in r.items()}
    assert hits["kara_yakin"] == {"NARR"}
    assert hits["ilk_kar"] == {"FIRST"}
    assert hits["hizlanan"] == {"ACCEL"}
    m = ideas.metrics(p, aj)
    assert m["q_be"][0] == pytest.approx(-(-12) / ((-12 - -40) / 3))   # straight-line quarters to break-even


def test_fscore_needs_all_inputs_and_improvement():
    m6 = 1e6
    # everything better in the last four quarters (index 6-9) than in the four before (2-5)
    good = {"net_income": [5 * m6] * 6 + [8 * m6] * 4, "revenue": [100 * m6] * 6 + [120 * m6] * 4,
            "gross_profit": [30 * m6] * 6 + [40 * m6] * 4, "assets": [1000 * m6] * 10, "liab": [600 * m6] * 6 + [500 * m6] * 4,
            "assets_cur": [300 * m6] * 6 + [350 * m6] * 4, "liab_cur": [200 * m6] * 10, "shares": [100.0] * 10}
    missing = {**good, "liab": [None] * 10}
    p = _panel({"GOOD": good, "MISS": missing}, Q)
    aj = np.full(2, len(Q) - 1)
    m = ideas.metrics(p, aj)
    assert m["fscore"][0] == 7 and m["f_known"][0] == 7
    r = ideas.rules(m)
    assert r["iyilesen"][0] and not r["iyilesen"][1]


def test_as_of_respects_publication_lag():
    m6 = 1e6
    p = _panel({"A": {"net_income": [1 * m6] * 10, "revenue": [10 * m6] * 10}}, Q)
    # CY2026Q2 ends 30 June: not public on 10 August (40 days), public on 25 August (56 days)
    assert p.q[ideas.as_of_index(p, date(2026, 8, 10))[0]] == "CY2026Q1"
    assert p.q[ideas.as_of_index(p, date(2026, 8, 25))[0]] == "CY2026Q2"
    assert p.q[ideas.as_of_index(p, date(2026, 7, 5), live=True)[0]] == "CY2026Q2"
    # a derived fiscal Q4 waits for the 10-K
    p.derived[0, len(Q) - 1] = True
    assert p.q[ideas.as_of_index(p, date(2026, 8, 25), p.derived)[0]] == "CY2026Q1"


def test_verdicts():
    def s(ex6, ex12, n=100):
        return {"all": {"126": {"n": n, "ex_uni": ex6}, "252": {"n": n, "ex_uni": ex12}}}
    bt = {"screens": {"kara_yakin": s(8, 14), "ilk_kar": s(2, 1), "hizlanan": s(0.5, 1), "iyilesen": s(9, 9, n=10)}}
    v = ideas.verdicts(bt)
    assert v["kara_yakin"][1] == "pos" and v["ilk_kar"][1] == "warn" and v["hizlanan"][1] == "neg" and v["iyilesen"][0] == "örnek az"


def test_page_empty_and_with_run(tmp_path, monkeypatch):
    import json
    from borsa_radar import db
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    from fastapi.testclient import TestClient
    from borsa_radar.web.app import app
    c = TestClient(app)
    assert "Henüz tarama yok" in c.get("/fikirler").text
    r = {"screen": "kara_yakin", "symbol": "NARR", "name": "Narrow Inc", "quarter_tr": "2Ç 2026", "price": 12.5, "mcap": 1.2e9,
         "trend_up": True, "ret6": 0.2, "ret6_spy": 0.08, "off_hi": -5.0, "rev_yoy": 0.3, "rev_yoy_prev": 0.25, "ni_yoy": None,
         "ni_q": [-12e6, -25e6, -35e6, -40e6, -44e6], "q_be": 1.3, "dil": 0.02, "fscore": 3, "ni_ttm": -112e6,
         "y": {"eps_next_y": 0.15, "n_analysts": 6, "rev_90d": 12.0, "rev_up30": 3, "rev_down30": 0, "ratings_up90": 2, "ratings_down90": 0,
               "insider_buys": 1, "insider_buy_usd": 250000.0, "news": [{"title": "Narrow wins contract", "day": "2026-11-10", "url": "https://example.com/a"}]}}
    rid = db.execute("INSERT INTO idea_runs(created_at, day, body) VALUES (?,?,?)",
                     ("2026-11-17T10:00:00+00:00", "2026-11-17", json.dumps({"day": "2026-11-17", "universe": 3881, "candidates": 50,
                                                                              "counts": {"kara_yakin": 1}})))
    db.execute("INSERT INTO ideas(run_id, day, screen, symbol, rank, shown, price, score, body) VALUES (?,?,?,?,?,?,?,?,?)",
               (rid, "2026-11-17", "kara_yakin", "NARR", 1, 1, 12.5, 70, json.dumps(r)))
    db.set_cursor(ideas.CURSOR_BT, json.dumps({"from": "2019-05-20", "to": "2026-08-20", "screens": {
        code: {"all": {h: {"n": 0} for h in ("63", "126", "252")}, "trend": {h: {"n": 0} for h in ("63", "126", "252")}} for code in ideas.SCREENS}}))
    t = c.get("/fikirler").text
    assert "NARR" in t and "Narrow wins contract" in t and "~1 çeyrekte başa baş" in t and "içeriden alım" in t
    assert "Fikir listesi" in c.get("/tickers").text
