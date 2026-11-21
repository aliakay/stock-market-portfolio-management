"""Strategy lab: indicator math and the trade rules shared by the backtest and the paper book."""

import numpy as np
import pytest

from borsa_radar import lab


def bars(closes, opens=None, highs=None, lows=None, start=20200101):
    n = len(closes)
    c = np.array(closes, dtype=float)
    o = np.array(opens if opens is not None else closes, dtype=float)
    h = np.array(highs if highs is not None else np.maximum(o, c), dtype=float)
    lo = np.array(lows if lows is not None else np.minimum(o, c), dtype=float)
    day = np.arange(start, start + n, dtype=float)
    return np.column_stack([day, o, h, lo, c, np.full(n, 1e7)])


def test_sma_and_rolling():
    x = np.arange(1, 11, dtype=float)
    s = lab.sma(x, 5)
    assert np.isnan(s[3]) and s[4] == 3 and s[-1] == 8
    assert lab.roll_min(x, 3)[2] == 1 and lab.roll_max(x, 3)[-1] == 10


def test_rsi_extremes():
    up = np.arange(1, 30, dtype=float)
    assert lab.rsi(up, 2)[-1] == 100
    down = up[::-1].copy()
    assert lab.rsi(down, 2)[-1] == pytest.approx(0, abs=1e-9)


def test_streak_and_pct_rank():
    c = np.array([1, 2, 3, 2, 1, 1, 2], dtype=float)
    assert list(lab.streak(c)) == [0, 1, 2, -1, -2, 0, 1]
    r = lab.pct_rank(np.array([1, 2, 3, 4, 0.5]), 4)
    assert r[4] == 0


def _ind(closes, **kw):
    return lab.Ind(bars(closes, **kw))


def test_follow_one_next_open_and_exit_rule():
    # 250 rising bars, then a 3-day dip, then a recovery: entry next open, exit at the open after close > SMA5
    c = list(np.linspace(50, 100, 250)) + [96, 93, 90, 95, 99, 101, 102]
    o = c[:]
    o[253] = 91.0       # entry bar open (signal at 252)
    x = _ind(c, opens=o)
    s = lab.Spec("t", "rsi2", 10, "sma5")
    t = 252
    assert lab.entry_mask(s, x)[t]
    tr = lab.follow_one(s, "X", x, t, lab.exit_mask(s, x))
    assert tr.entry == 91.0 and tr.entry_day == x.day[253]
    assert tr.why == "signal" and tr.exit_day > tr.entry_day
    assert tr.ret == pytest.approx(tr.exit / tr.entry - 1 - lab.COST_RT)


def test_limit_order_lapses_when_not_reached():
    c = list(np.linspace(50, 100, 250)) + [95, 92, 91.5]
    x = _ind(c, lows=None)
    s = lab.Spec("t", "rsi2", 50, "sma5", mode="limit", limit=0.05)
    # signal at bar 251 (close 92) → limit 87.4; next bar low 91.5 → no fill
    assert lab.follow_one(s, "X", x, 251, lab.exit_mask(s, x)) is None


def test_open_trade_at_data_end_and_time_exit():
    c = list(np.linspace(50, 100, 250)) + [95, 90, 89, 88]
    x = _ind(c)
    s = lab.Spec("t", "rsi2", 10, "sma5", hold=2)
    tr = lab.follow_one(s, "X", x, 251, lab.exit_mask(s, x))
    assert tr.why == "exit_next"          # two sessions held, time is up: out at the next open
    s3 = lab.Spec("t", "rsi2", 10, "sma5", hold=5)
    assert lab.follow_one(s3, "X", x, 251, lab.exit_mask(s3, x)).why == "open"


def test_stop_fills_at_open_on_gap():
    c = list(np.linspace(50, 100, 250)) + [95, 90, 88, 72, 71]
    o = c[:]
    o[252] = 89.0       # entry bar: open 89, low 88 (stop 84.55 not touched)
    o[253] = 70.0       # next bar gaps far below the stop: filled at the open, not at the stop
    lows = [min(a, b) for a, b in zip(o, c)]
    x = _ind(c, opens=o, lows=lows)
    s = lab.Spec("t", "rsi2", 10, "sma5", stop=0.05)
    tr = lab.follow_one(s, "X", x, 251, lab.exit_mask(s, x))
    assert tr.why == "stop" and tr.exit == 70.0
    # stop touched inside the entry bar itself: filled at the stop level
    lows2 = lows[:]
    lows2[252] = 80.0
    x2 = _ind(c, opens=o, lows=lows2)
    tr2 = lab.follow_one(s, "X", x2, 251, lab.exit_mask(s, x2))
    assert tr2.why == "stop" and tr2.exit == pytest.approx(89.0 * 0.95)


def test_stats_and_agg_agree():
    trades = [lab.Trade("A", 20100104, 20100105, 10, 20100110, 11, 0.099, 3, 1, "signal"),
              lab.Trade("A", 20180104, 20180105, 10, 20180110, 9, -0.101, 3, 1, "time"),
              lab.Trade("B", 20180204, 20180205, 10, 20180210, 10, 0.0, 3, 1, "open")]
    st = lab.stats(trades)
    assert st["n"] == 2 and st["win"] == 50
    a = lab.Agg()
    a.add(trades)
    assert a.summary()["n"] == 2
    assert a.summary(20170101)["n"] == 1 and a.summary(20170101)["win"] == 0


def test_spec_key_is_stable():
    assert lab.Spec("x", "crsi", 10, "crsi", mode="limit", limit=0.03).key == "crsi|10|crsi|limit3|h10|none"


def test_paper_follow_matches_backtest(monkeypatch):
    """The paper book replays a live signal with lab.follow_one: pending → lapsed / open / closed."""
    from borsa_radar import strateji
    spec = lab.Spec("t", "rsi2", 10, "sma5", mode="limit", limit=0.03)
    monkeypatch.setattr(strateji, "STRATEGIES", [strateji.Strategy("t", "Test", spec, "", "", "")])
    c = list(np.linspace(50, 100, 250)) + [95, 90]
    x = _ind(c)
    row = {"strategy": "t", "symbol": "X", "sig_day": strateji._d(int(x.day[-1])), "rank": 5.0}
    assert strateji.follow(row, x) == {"status": "pending"}
    # next session: low 86 reaches the 87.3 limit, close 88 → open position
    c2 = c + [88]
    x2 = _ind(c2, lows=[min(a, b) for a, b in zip(c2, c2)][:-1] + [86], opens=c2[:-1] + [89])
    st = strateji.follow(row, x2)
    assert st["status"] == "open" and st["entry_px"] == pytest.approx(90 * 0.97)
    # next session never trades down to the limit → lapsed
    c3 = c + [91]
    x3 = _ind(c3, lows=c3[:-1] + [89.5], opens=c3[:-1] + [90.5])
    assert strateji.follow(row, x3) == {"status": "lapsed"}


@pytest.fixture
def lab_client(tmp_path, monkeypatch):
    from borsa_radar import db
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    from fastapi.testclient import TestClient
    from borsa_radar.web.app import app
    return TestClient(app)


def _res():
    st = {"n": 600, "win": 80.8, "avg": 2.17, "avg_win": 4.08, "avg_loss": -5.85, "pf": 2.94, "worst": -32.9, "p5": -7.8, "hold": 4.6,
          "years": {"2017": {"n": 20, "win": 85.0, "avg": 1.8}, "2022": {"n": 20, "win": 55.0, "avg": -0.7}}}
    book = {"cagr": 7.1, "max_dd": 14.2, "exposure": 47.0, "taken": {"n": 490, "win": 78.4}, "curve": [[20170131, 1.0], [20170228, 1.02], [20170331, 1.05]]}
    spy = {"cagr": 14.6, "max_dd": 33.7, "curve": [[20170131, 1.0], [20170228, 1.03], [20170331, 1.04]]}
    row = {"code": "derin", "name": "Derin düşüş alımı", "key": "crsi|15|above_entry|limit6|h30|all", "what": "Fikir.", "entry_tr": "Giriş kuralı.",
           "exit_tr": "Çıkış kuralı.", "all": st}
    for w in ("train", "test"):
        row.update({w: st, w + "_stress": {**st, "win": 80.0}, w + "_book": book, w + "_spy": spy})
    return {"created_at": "2026-11-17T12:00:00+00:00", "data_through": "2026-11-16", "universe": 907, "cost_rt": 0.1,
            "train_end": "2016-12-31", "strategies": [row], "today": "2026-11-17"}


def test_lab_page_empty_and_full(lab_client, monkeypatch):
    from borsa_radar import db, strateji
    r = lab_client.get("/setups/strateji")
    assert r.status_code == 200 and "Henüz geriye dönük test sonucu yok" in r.text
    import json
    db.set_cursor(strateji.RESULTS_KEY, json.dumps(_res()))
    db.execute("INSERT INTO paper_trades(strategy, symbol, sig_day, tier, rank, limit_px, status, in_book, body, created_at) "
               "VALUES ('derin', 'ABC', '2026-11-16', 'kesif', 3.2, 94.0, 'pending', 1, ?, '2026-11-17')",
               (json.dumps({"company": "Abc Corp", "close": 100.0}),))
    db.execute("INSERT INTO paper_trades(strategy, symbol, sig_day, status, in_book, entry_day, entry_px, exit_day, exit_px, ret, why, created_at) "
               "VALUES ('derin', 'XYZ', '2026-10-01', 'closed', 1, '2026-10-02', 50, '2026-10-06', 52, 3.9, 'signal', '2026-10-01')")
    r = lab_client.get("/setups/strateji")
    t = r.text
    assert r.status_code == 200
    assert "80,8" in t and "Derin düşüş alımı" in t and "limit alış <b>94,00 $</b>" in t and "defterde" in t
    assert "XYZ" in t and "çıkış kuralı" in t and "<polyline" in t
    assert "Strateji laboratuvarı" in lab_client.get("/setups").text   # sub-tab


def test_paper_book_cycle(lab_client, monkeypatch):
    """scan → pending (book slot) → follow on the next bars → open → closed, with the lab's own rules."""
    from borsa_radar import db, strateji
    spec = lab.Spec("t", "rsi2", 10, "sma5", mode="limit", limit=0.03, hold=10)
    monkeypatch.setattr(strateji, "STRATEGIES", [strateji.Strategy("t", "Test", spec, "", "", "")])
    monkeypatch.setattr(strateji, "live_macro", lambda: None)
    c = list(np.linspace(50, 100, 250)) + [95, 90]
    state = {"bars": bars(c)}
    monkeypatch.setattr(strateji, "_ind", lambda sym, until=None: lab.Ind(state["bars"]))
    from borsa_radar import setups
    monkeypatch.setattr(setups, "universe", lambda wide=False: [{"symbol": "ABC", "tier": "liste", "src": "izleme"}])
    day = strateji._d(int(state["bars"][-1, 0]))
    monkeypatch.setattr(setups, "base_day", lambda: day)
    assert strateji.scan()["signals"] == 1
    row = dict(db.query_one("SELECT * FROM paper_trades"))
    assert row["status"] == "pending" and row["in_book"] == 1 and row["limit_px"] == pytest.approx(90 * 0.97, abs=0.01)
    # fill next session (low 86), then a recovery above the 5-day average → exit at the following open
    c2 = c + [88, 96, 99]
    state["bars"] = bars(c2, lows=c2[:-3] + [86, 95, 98], opens=c2[:-3] + [89, 92, 97])
    strateji.update_open()
    row = dict(db.query_one("SELECT * FROM paper_trades"))
    assert row["status"] == "closed" and row["entry_px"] == pytest.approx(87.3) and row["exit_px"] == 97 and row["why"] == "signal"
    assert strateji.paper_record()["t"]["all"]["n"] == 1
