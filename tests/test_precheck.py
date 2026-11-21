import json
from datetime import date, timedelta

import pytest

from borsa_radar import db, portfolio, precheck, regime, risk, stockdata, technicals
from borsa_radar.util import iso, utcnow


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    portfolio.invalidate()
    risk._CACHE.clear()
    # no network: sectors, Yahoo calendar, bar refresh
    monkeypatch.setattr(risk, "_sector", lambda s: ("Technology", "Semiconductors") if s in ("AAA", "BBB") else ("Energy", "Oil"))
    monkeypatch.setattr(stockdata, "yahoo", lambda *a, **k: stockdata.Fetched(None, None, "Yahoo"))
    monkeypatch.setattr(technicals, "ensure_bars", lambda *a, **k: [])
    monkeypatch.setattr(regime, "latest", lambda: {"day": DAYS[-1], "overall": 64.0, "label": "Temkinli Boğa"})


TODAY = date.today()


def _weekdays(n: int, end: date) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


DAYS = _weekdays(160, TODAY - timedelta(days=1))


def _seed(symbol, closes, spread=0.02):
    with db.tx() as c:
        for day, close in zip(DAYS, closes):
            c.execute("INSERT INTO prices_daily(symbol, day, open, high, low, close, source, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (symbol, day, close, close * (1 + spread), close * (1 - spread), close, "test", "x"))


def _wave(base, amp, phase=0.0):
    import math
    return [base * (1 + amp * math.sin(k / 3 + phase) + 0.001 * k) for k in range(len(DAYS))]


def _setup_book():
    _seed("SPY", _wave(500, 0.01))
    _seed("AAA", _wave(100, 0.05))
    _seed("BBB", _wave(50, 0.05, 0.1))      # moves with AAA
    _seed("XOM", _wave(100, 0.05, 2.5))     # out of phase
    portfolio.add_cash(kind="deposit", amount=10_000, sub="P1", ts=DAYS[0] + "T14:00:00Z")
    portfolio.add_trade(side="BUY", symbol="AAA", qty=40, price=100, sub="P1", ts=DAYS[0] + "T15:00:00Z")
    portfolio.invalidate()


def _by_key(r):
    return {c["key"]: c for c in r["checks"]}


def test_buy_sizing_with_given_stop():
    _setup_book()
    r = precheck.check("buy", "XOM", qty=10, price=100, stop=95)
    total = portfolio.build().total[-1]["value"]
    assert r["budget"] == pytest.approx(total * 0.01)
    assert r["per_share_risk"] == pytest.approx(5)
    assert r["max_by_risk"] == pytest.approx(total * 0.01 / 5)
    assert r["planned_risk"] == pytest.approx(50)
    c = _by_key(r)
    assert c["size"]["status"] == "ok"
    assert c["weight"]["status"] == "ok"          # 1000 / ~10k ≈ %10
    assert "%" in c["size"]["reason"] and "$" in c["size"]["reason"]
    # too big: risk 3× budget → DUR
    r2 = precheck.check("buy", "XOM", qty=60, price=100, stop=95)
    assert _by_key(r2)["size"]["status"] == "stop"
    assert r2["verdict"] == "DUR"


def test_stop_falls_back_to_atr_and_suggests_qty():
    _setup_book()
    r = precheck.check("buy", "XOM")
    assert r["stop_src"].startswith("fiyat − 2×ATR(14)")
    assert r["stop"] == pytest.approx(r["price"] - 2 * r["atr"])
    assert r["qty_src"].startswith("önerilen")
    assert r["suggested_qty"] == pytest.approx(min(r["max_by_risk"], r["max_by_weight"]), abs=1.0)
    assert r["suggested_qty"] <= min(r["max_by_risk"], r["max_by_weight"])


def test_invalid_stop_above_price_is_stop():
    _setup_book()
    r = precheck.check("buy", "XOM", qty=1, price=100, stop=101)
    assert _by_key(r)["size"]["status"] == "stop"


def test_weight_sector_and_cluster_when_adding_to_correlated():
    _setup_book()
    r = precheck.check("buy", "BBB", amount=3000, price=50, stop=45)
    c = _by_key(r)
    assert c["sector"]["status"] == "warn"        # AAA + BBB Technology > %40
    assert c["cluster"]["status"] == "warn"       # BBB ~ AAA correlated, cluster > %40
    assert r["cluster"]["symbols"] == ["AAA"]
    assert r["correlations"][0]["symbol"] == "AAA" and r["correlations"][0]["corr"] > 0.9
    r2 = precheck.check("buy", "XOM", qty=5, price=100, stop=95)
    assert _by_key(r2)["cluster"]["status"] == "ok"


def test_regime_ceiling_warns_when_over():
    _setup_book()
    # stock ~40% now; buying 5000 takes it over the 80% ceiling for score 64
    r = precheck.check("buy", "XOM", amount=5000, price=100, stop=90)
    assert r["regime"]["ceiling"] == 80
    assert _by_key(r)["regime"]["status"] == "warn"


def test_plan_needs_thesis_with_exit_rule():
    _setup_book()
    r = precheck.check("buy", "XOM", qty=1, price=100, stop=95)
    c = _by_key(r)["plan"]
    assert c["status"] == "warn" and "plansız giriş" in c["reason"].lower() and c["link"] == "/tez?symbol=XOM"
    from borsa_radar import thesis
    thesis.add("XOM", "Enerji fiyatları toparlanır.", rules=["stop 90"], status="idea")
    r = precheck.check("buy", "XOM", qty=1, price=100, stop=95)
    assert _by_key(r)["plan"]["status"] == "ok"


def test_circuit_breaker_streak_and_revenge():
    _setup_book()
    d1, d2 = DAYS[-3], DAYS[-1]
    for sym, buy_px, sell_px, d in (("XOM", 100, 90, d1), ("BBB", 50, 40, d2)):
        portfolio.add_trade(side="BUY", symbol=sym, qty=10, price=buy_px, sub="P1", ts=DAYS[-10] + "T15:00:00Z")
        portfolio.add_trade(side="SELL", symbol=sym, qty=10, price=sell_px, sub="P1", ts=d + "T15:00:00Z")
    portfolio.invalidate()
    r = precheck.check("buy", "XOM", qty=1, price=100, stop=95)
    c = _by_key(r)
    assert c["breaker"]["status"] == "stop" and "Soğuma" in c["breaker"]["reason"]
    assert c["revenge"]["status"] == "warn"
    assert "Aynı hisseye" not in c["revenge"]["reason"] or "XOM" in c["revenge"]["reason"]
    assert r["verdict"] == "DUR"
    assert r["realized_7d"] == pytest.approx(-200)


def test_weekly_loss_halt():
    _setup_book()
    portfolio.add_trade(side="SELL", symbol="AAA", qty=40, price=80, sub="P1", ts=DAYS[-1] + "T15:00:00Z")  # −800 realized
    portfolio.invalidate()
    r = precheck.check("buy", "XOM", qty=1, price=100, stop=95)
    c = _by_key(r)["breaker"]
    assert c["status"] == "stop" and "7 gün" in c["reason"]


def test_earnings_within_five_days_warns():
    _setup_book()
    when = utcnow() + timedelta(days=1)
    db.execute("INSERT INTO macro_calendar(key, name, release_at, importance) VALUES (?,?,?,4)",
               ("earnings:XOM", "XOM bilanço (kapanış sonrası)", iso(when)))
    r = precheck.check("buy", "XOM", qty=1, price=100, stop=95)
    c = _by_key(r)["earnings"]
    assert c["status"] == "warn" and "XOM bilanço" in c["reason"]
    r2 = precheck.check("buy", "BBB", qty=1, price=50, stop=45)
    assert _by_key(r2)["earnings"]["status"] == "na"


def test_sell_estimate_and_oversell():
    _setup_book()
    px = portfolio.positions()[0]["price"]
    r = precheck.check("sell", "AAA", qty=10)
    assert r["est_realized"] == pytest.approx((px - 100) * 10)
    assert _by_key(r)["position"]["status"] == "ok"
    assert r["hold_days"] == (date.today() - date.fromisoformat(DAYS[0])).days
    assert precheck.check("sell", "AAA")["qty"] == pytest.approx(40)       # default: whole position
    assert _by_key(precheck.check("sell", "AAA", qty=50))["position"]["status"] == "stop"
    assert precheck.check("sell", "ZZZ", qty=1)["verdict"] == "DUR"


def test_sell_quick_winner_without_target_rule_warns():
    _setup_book()
    portfolio.add_trade(side="BUY", symbol="BBB", qty=10, price=10, sub="P1", ts=DAYS[-3] + "T15:00:00Z")
    portfolio.invalidate()
    from borsa_radar import thesis
    thesis.add("BBB", "Kısa vadeli momentum.", rules=["stop 5"])
    r = precheck.check("sell", "BBB", price=60)
    assert _by_key(r)["plan"]["status"] == "warn"


def test_brief_is_json_and_validation():
    _setup_book()
    b = precheck.brief(precheck.check("buy", "XOM", qty=2, price=100, stop=95))
    json.dumps(b)
    assert b["verdict"] in ("GEÇ", "DİKKAT", "DUR") and b["checks"]
    with pytest.raises(ValueError):
        precheck.check("hold", "XOM")
    with pytest.raises(ValueError):
        precheck.check("buy", "XOM", qty=-1)


def test_web_page_renders(monkeypatch):
    _setup_book()
    from fastapi.testclient import TestClient

    from borsa_radar.web.app import app
    cl = TestClient(app)   # no `with`: lifespan (scheduler) is not started
    assert cl.get("/portfolio/kontrol").status_code == 200
    res = cl.get("/portfolio/kontrol", params={"side": "buy", "symbol": "xom", "qty": "2", "price": "100", "stop": "95"})
    assert res.status_code == 200 and "Kontroller" in res.text and "XOM" in res.text
    res = cl.get("/portfolio/kontrol", params={"side": "sell", "symbol": "AAA"})
    assert res.status_code == 200 and "Tahmini gerçekleşen" in res.text
    res = cl.get("/portfolio/kontrol", params={"side": "buy", "symbol": "XOM", "qty": "abc"})
    assert res.status_code == 200 and "Sayı okunamadı" in res.text
