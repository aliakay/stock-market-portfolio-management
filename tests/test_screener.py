"""Stock screener: technical fields on a hand-made series, filters, presets, the listing table and the page."""

import csv
import io
import json
import zipfile
from datetime import date, timedelta

import numpy as np
import pytest

from borsa_radar import screener


def test_technicals_hand_case():
    n = 260
    c = np.linspace(50, 102, n)          # steady rise
    c[-1] = 100.0                         # a small pull-back on the last day
    h, lo, v = c * 1.01, c * 0.99, np.full(n, 1e6)
    v[-1] = 3e6
    days = [(date(2025, 11, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    t = screener.technicals(c, h, lo, v, days, spy_r6=10.0)
    assert t["price"] == 100.0
    assert t["r1m"] == pytest.approx((100 / c[-22] - 1) * 100)
    assert t["off_hi"] == pytest.approx((100 / h.max() - 1) * 100)
    assert t["trend"] == "guclu"                                   # price > 50-day > 200-day
    assert t["relvol"] == pytest.approx(3.0)                       # today's volume / previous 20 days
    assert t["dvol"] == pytest.approx((c[-20:] * v[-20:]).mean())
    assert t["rs6"] == pytest.approx(t["r6m"] - 10.0)
    assert screener.technicals(c[:20], h[:20], lo[:20], v[:20], days[:20], None) is None   # too short


ROWS = [
    {"symbol": "AAA", "name": "Alpha Software", "industry": "Software - Application", "sector": "Technology", "mcap": 12.0, "rev_yoy": 30.0,
     "ni_yoy": 40.0, "accel": True, "ni_ttm": 50.0, "trend": "guclu", "off_hi": -3.0, "rules": ["hizlanan"], "earn": None, "rsi": 60},
    {"symbol": "BBB", "name": "Beta Bio", "sector": "Healthcare", "mcap": 0.8, "rev_yoy": 55.0, "ni_ttm": -20.0, "narrowing": True,
     "trend": "yukari", "off_hi": -20.0, "rules": ["kara_yakin"], "earn": (date.today() + timedelta(days=5)).isoformat(), "rsi": 25},
    {"symbol": "CCC", "name": "Gamma Steel", "sector": "Basic Materials", "mcap": 3.0, "rev_yoy": -5.0, "ni_ttm": 10.0, "trend": "asagi",
     "off_hi": -45.0, "rules": [], "earn": None, "rsi": None, "adr": True},
]


def test_filters():
    assert [r["symbol"] for r in screener.apply(ROWS, {"rev_yoy_min": "20"})] == ["AAA", "BBB"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"mcap_max": "5", "profit": "kar"})] == ["CCC"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"trend": "yukari"})] == ["AAA", "BBB"]     # strong trend counts as up
    assert [r["symbol"] for r in screener.apply(ROWS, {"profit": "daralan"})] == ["BBB"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"rule": "kara_yakin"})] == ["BBB"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"earn": "7"})] == ["BBB"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"q": "software"})] == ["AAA"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"rsi_max": "30"})] == ["BBB"]            # missing values never match a bound
    assert [r["symbol"] for r in screener.apply(ROWS, {"noadr": "1", "accel": "1"})] == ["AAA"]
    assert [r["symbol"] for r in screener.apply(ROWS, {"off_hi_min": "-10,5"})] == ["AAA"]        # Turkish decimal comma


def test_sort_and_presets():
    assert [r["symbol"] for r in screener.sort(ROWS, "rsi", desc=False)] == ["BBB", "AAA", "CCC"]   # missing last
    assert [r["symbol"] for r in screener.sort(ROWS, "nonsense")] == ["AAA", "CCC", "BBB"]        # falls back to market cap
    flt, key, desc, pre = screener.resolve({"preset": "sert_dusen", "mcap_min": "2"})
    assert flt == {"mcap_min": "2", "off_hi_max": "-30"} and key == "off_hi" and not desc and pre["name"]
    choice = {"q", "sector", "trend", "profit", "accel", "noadr", "rule", "earn"}
    for code, p in screener.PRESETS.items():
        for k in p["q"]:
            assert k in choice or (k.rsplit("_", 1)[0] in screener.FIELD and k.rsplit("_", 1)[1] in ("min", "max")), (code, k)
        assert p["sort"] in screener.SORTABLE


def test_csv_export():
    rows = list(csv.reader(io.StringIO(screener.to_csv(ROWS))))
    assert rows[0][0] == "symbol" and rows[1][0] == "AAA" and rows[1][-1] == "hizlanan"


def test_listing_table_filters(tmp_path, monkeypatch):
    cols = ["table", "ticker", "name", "exchange", "isdelisted", "category", "siccode", "sector", "industry"]
    data = [["SEP", "AAPL", "Apple", "NASDAQ", "N", "Domestic Common Stock", "3571", "Technology", "Consumer Electronics"],
            ["SEP", "BRK.B", "Berkshire", "NYSE", "N", "Domestic Common Stock Secondary Class", "6331", "Financial Services", "Insurance"],
            ["SEP", "TSM", "TSMC", "NYSE", "N", "ADR Common Stock", "3674", "Technology", "Semiconductors"],
            ["SEP", "SPAC", "Shell Acq", "NASDAQ", "N", "Domestic Common Stock", "6770", "Financial Services", "Shell Companies"],
            ["SEP", "UNIT.U", "Unit", "NYSE", "N", "Domestic Common Stock", "1000", "", ""],
            ["SEP", "OTCX", "Otc Co", "OTC", "N", "Domestic Common Stock", "1000", "", ""],
            ["SEP", "PFD", "Pref", "NYSE", "N", "Domestic Preferred Stock", "1000", "", ""],
            ["SEP", "GONE", "Gone", "NYSE", "Y", "Domestic Common Stock", "1000", "", ""],
            ["SF1", "AAPL", "Apple", "NASDAQ", "N", "Domestic Common Stock", "3571", "Technology", "Consumer Electronics"]]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(data)
    f = tmp_path / "t.csv.zip"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("tickers.csv", buf.getvalue())
    monkeypatch.setattr(screener, "TICKERS_FILE", f)
    monkeypatch.setattr(screener.config, "secret", lambda name: None)
    t = screener._tickers_table()
    assert set(t) == {"AAPL", "BRK-B", "TSM"} and t["TSM"]["adr"] and t["BRK-B"]["sector"] == "Financial Services"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from borsa_radar import db
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    screener._cache.clear()
    from fastapi.testclient import TestClient
    from borsa_radar.web.app import app
    return TestClient(app), db


def test_page(client):
    c, db = client
    assert "Tarama verisi henüz yok" in c.get("/tarayici").text
    rid = db.execute("INSERT INTO screener_runs(created_at, day, body) VALUES (?,?,?)",
                     ("2026-11-18T03:00:00+00:00", date.today().isoformat(),
                      json.dumps({"day": date.today().isoformat(), "price_day": "2026-11-17", "n": 3, "universe": 3, "with_fund": 3, "with_earn": 1})))
    for r in ROWS:
        db.execute("INSERT INTO screener_rows(run_id, symbol, body) VALUES (?,?,?)", (rid, r["symbol"], json.dumps(r)))
    screener._cache.clear()
    t = c.get("/tarayici").text
    assert "3 hisse" in t and "AAA" in t and "Hazır taramalar" in t and "Kâra yaklaşan" in t
    t = c.get("/tarayici?preset=kara_yakin").text
    assert "1 hisse" in t and "BBB" in t and "geçmişte ölçüldü" in t
    t = c.get("/tarayici?sort=rsi&dir=asc&rev_yoy_min=20").text
    assert "2 hisse" in t and t.index("BBB") < t.index("AAA")
    r = c.get("/tarayici.csv?rev_yoy_min=20")
    assert r.status_code == 200 and r.text.splitlines()[0].startswith("symbol") and len(r.text.splitlines()) == 3
    r = c.post("/tarayici/kaydet", data={"name": "Büyüyenler", "query": "rev_yoy_min=20"}, follow_redirects=False)
    assert r.status_code == 303 and screener.saved()[0]["name"] == "Büyüyenler"
    assert "Büyüyenler" in c.get("/tarayici").text
    c.post(f"/tarayici/sil/{screener.saved()[0]['id']}")
    assert screener.saved() == []
    assert "Tarayıcı" in c.get("/tickers").text                    # sub-tab in the nav


def test_frames_fill_gaps_after_a_tag_switch():
    from borsa_radar import frames
    nan = np.nan
    # company 0 switched revenue tags after quarter 2; concept B also carries a small sub-line for company 1
    a = np.array([[100.0, 110.0, nan, nan], [50.0, 52.0, 54.0, nan]])
    b = np.array([[nan, nan, 125.0, 130.0], [nan, nan, nan, 5.0]])
    arr, der = a.copy(), np.zeros_like(a, dtype=bool)
    n = frames.fill_gaps("revenue", arr, der, [(a, np.zeros_like(der)), (b, np.zeros_like(der))])
    assert n == 2 and list(arr[0]) == [100.0, 110.0, 125.0, 130.0]
    assert np.isnan(arr[1, 3])                        # 5 next to 54 is a sub-line, not total revenue
    ni_a = np.array([[nan, -3.0]])
    ni_b = np.array([[7.0, -2.5]])
    arr = ni_a.copy()
    frames.fill_gaps("net_income", arr, np.zeros_like(arr, dtype=bool), [(ni_a, np.zeros((1, 2), bool)), (ni_b, np.zeros((1, 2), bool))])
    assert list(arr[0]) == [7.0, -3.0]


def test_beta_and_portfolio_correlation_hand_case():
    rng = np.random.default_rng(1)
    mkt = rng.normal(0, 0.01, 250)
    other = rng.normal(0, 0.01, 250)
    b, c = screener.beta_corr(2 * mkt, mkt, port=mkt)
    assert b == pytest.approx(2.0) and c == pytest.approx(1.0)
    b, c = screener.beta_corr(0.5 * mkt + other, mkt, port=-mkt)
    assert b == pytest.approx(0.5, abs=0.15) and c < -0.2                      # moves against the portfolio
    x = 2 * mkt
    x[:200] = np.nan                                                          # a recent listing: too few days
    assert screener.beta_corr(x, mkt, None) == (None, None)
    assert screener.beta_corr(2 * mkt, mkt, None)[1] is None                  # no portfolio → no correlation
    cal = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    r = screener.daily_returns(["2026-11-16", "2026-11-17", "2026-11-19"], np.array([100.0, 110.0, 99.0]), cal)
    assert r[0] == pytest.approx(0.10) and np.isnan(r[1]) and np.isnan(r[2])  # a missing day breaks both returns around it


def _yahoo_recs(cols: list[str], **lines) -> list[dict]:
    """stockdata.yahoo('quarterly_income_stmt') records: one dict per line item, a key per quarter-end timestamp."""
    return [{"index": name.replace("_", " "), **{f"{c} 00:00:00": v for c, v in zip(cols, vals)}} for name, vals in lines.items()]


def test_yahoo_fallback_hand_case():
    cols = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    recs = _yahoo_recs(cols, Total_Revenue=[120e6, 110e6, 100e6, 90e6, 80e6, None], Net_Income=[-2e6, -4e6, -6e6, -8e6, -9e6, None],
                       Operating_Income=[1e6, 0.0, -1e6, -2e6, -3e6, None], Gross_Profit=[48e6, 44e6, 40e6, 36e6, 32e6, None],
                       Diluted_Average_Shares=[50e6, 49e6, 48e6, 47e6, 46e6, None])
    f = screener.yahoo_fund(recs, date(2026, 11, 18))
    assert f["src"] == "yahoo" and f["quarter"] == "3Ç 2026"
    assert f["rev_ttm"] == 420.0 and f["ni_ttm"] == -20.0 and f["ni_q0"] == -2.0
    assert f["rev_yoy"] == 50.0 and f["rev_yoy_prev"] is None          # no quarter a year before Q2 2026
    assert f["ni_yoy"] is None                                          # a loss a year ago: no growth rate
    assert f["om"] == pytest.approx(-2 / 420 * 100, abs=0.05) and f["gm"] == 40.0 and f["shares"] == 50e6
    assert f["narrowing"] and f["losses4"] == 4 and f["q_be"] == 1.0    # −2 now, +2 a quarter → one quarter to break-even
    assert f["fscore"] is None and f["rules"] == []
    assert screener.yahoo_fund(recs, date(2027, 6, 1)) is None          # newest quarter too old
    holed = _yahoo_recs(["2026-09-30", "2026-06-30", "2025-12-31", "2025-09-30"], Total_Revenue=[4.0, 3.0, 2.0, 1.0], Net_Income=[1.0] * 4)
    f = screener.yahoo_fund(holed, date(2026, 11, 18))
    assert f["rev_ttm"] is None and f["rev_yoy"] == 300.0               # a missing quarter: no year sum, the year-ago quarter still counts
    assert screener.yahoo_fund(None, date(2026, 11, 18)) is None
