import pytest

from borsa_radar import db, portfolio
from borsa_radar.importers import revolut


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    portfolio.invalidate()


def _bars(symbol, closes):
    """Seed daily closes: {day: close}."""
    with db.tx() as c:
        for day, close in closes.items():
            c.execute("INSERT INTO prices_daily(symbol, day, close, source, updated_at) VALUES (?,?,?,?,?)",
                      (symbol, day, close, "test", "x"))


DAYS = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]


def test_average_cost_realized_and_closed_trip():
    _bars("SPY", dict.fromkeys(DAYS, 100.0))
    _bars("AAA", dict(zip(DAYS, [10.0, 12.0, 15.0, 15.0])))
    portfolio.add_cash(kind="deposit", amount=1000, sub="P1", ts="2026-03-02T14:00:00Z")
    portfolio.add_trade(side="BUY", symbol="AAA", qty=10, price=10, sub="P1", ts="2026-03-02T15:00:00Z")
    portfolio.add_trade(side="BUY", symbol="AAA", qty=10, price=12, sub="P1", ts="2026-03-03T15:00:00Z")
    sell = portfolio.add_trade(side="SELL", symbol="AAA", qty=5, price=15, sub="P1", ts="2026-03-04T15:00:00Z")
    led = portfolio.build(force=True)
    st = led.trade_state[sell]
    assert st["qty_after"] == pytest.approx(15)
    assert st["avg_after"] == pytest.approx(11)          # (100 + 120) / 20
    assert st["realized"] == pytest.approx(5 * 15 - 5 * 11)
    pos = portfolio.positions(led=led)
    assert pos[0]["value"] == pytest.approx(15 * 15)
    assert led.cash[portfolio.resolve_sub("P1")["id"]] == pytest.approx(1000 - 100 - 120 + 75)
    portfolio.add_trade(side="SELL", symbol="AAA", qty=15, price=15, sub="P1", ts="2026-03-05T15:00:00Z")
    closed = portfolio.closed_positions(portfolio.build(force=True))
    assert len(closed) == 1
    assert closed[0]["realized"] == pytest.approx((75 + 225) - (100 + 120))
    assert closed[0]["days"] == 3


def test_time_weighted_return_ignores_deposits():
    _bars("SPY", dict.fromkeys(DAYS, 100.0))
    _bars("AAA", dict(zip(DAYS, [10.0, 11.0, 11.0, 12.1])))
    portfolio.add_cash(kind="deposit", amount=100, sub="P1", ts="2026-03-02T14:00:00Z")
    portfolio.add_trade(side="BUY", symbol="AAA", qty=10, price=10, sub="P1", ts="2026-03-02T15:00:00Z")
    # big deposit mid-way must not show up as return
    portfolio.add_cash(kind="deposit", amount=10_000, sub="P1", ts="2026-03-04T14:00:00Z")
    perf = portfolio.total_perf(portfolio.build(force=True))
    # holdings went 10 → 12.1 (+21%) while 100 was invested; afterwards mostly cash
    day2 = (11 / 10 - 1)
    day4 = (10_000 + 121 - 10_000 - 110) / (110 + 10_000)
    assert perf["periods"]["all"]["pct"] == pytest.approx(((1 + day2) * (1 + day4) - 1) * 100)
    assert perf["periods"]["all"]["pnl"] == pytest.approx(21)


def test_split_adjusts_old_quantities():
    _bars("SPY", dict.fromkeys(DAYS, 100.0))
    _bars("BBB", dict.fromkeys(DAYS, 10.0))
    db.execute("INSERT INTO splits(symbol, day, ratio) VALUES ('BBB', '2026-03-04', 10)")
    portfolio.add_trade(side="BUY", symbol="BBB", qty=2, price=100, sub="P1", ts="2026-03-02T15:00:00Z")
    pos = portfolio.positions(led=portfolio.build(force=True))
    assert pos[0]["qty"] == pytest.approx(20) and pos[0]["avg"] == pytest.approx(10)


def test_implicit_funding_when_no_deposit():
    _bars("SPY", dict.fromkeys(DAYS, 100.0))
    _bars("AAA", dict.fromkeys(DAYS, 10.0))
    portfolio.add_trade(side="BUY", symbol="AAA", qty=5, price=10, sub="P2", ts="2026-03-02T15:00:00Z")
    led = portfolio.build(force=True)
    assert sum(led.cash.values()) == pytest.approx(0)
    assert sum(led.implicit.values()) == pytest.approx(50)


def test_option_symbol_parsing_and_multiplier():
    tid = portfolio.add_trade(side="BUY", symbol="AMZN271217C00270000", qty=2, price=12.5, sub="P1",
                              ts="2026-03-02T15:00:00Z")
    t = db.query_one("SELECT * FROM trades WHERE id = ?", (tid,))
    assert (t["asset"], t["underlying"], t["opt_type"], t["strike"], t["expiry"], t["multiplier"]) == \
        ("option", "AMZN", "C", 270.0, "2027-12-17", 100.0)


CSV = """Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate
2026-03-02T10:00:00.000Z,,CASH TOP-UP,,,USD 1000,USD,1.08
2026-03-02T15:31:10.123Z,AAA,BUY - MARKET,10,USD 10.00,USD 100.50,USD,1.08
2026-03-03T15:00:00.000Z,BRK.B,BUY - LIMIT,1,USD 400,USD 400,USD,1.08
2026-03-04T15:00:00.000Z,AAA,SELL - MARKET,4,USD 12.00,USD 48.00,USD,1.08
2026-03-04T16:00:00.000Z,AAA,DIVIDEND,,,USD 0.40,USD,1.08
2026-03-04T17:00:00.000Z,AAA,STOCK SPLIT,10,,,USD,1.08
2026-03-05T16:27:27.419Z,ZZZR,MERGER - CASH,,,USD 13.64,USD,1.14
2026-03-05T16:27:27.483Z,ZZZR,MERGER - STOCK,-248.18,,USD 0,USD,1.14
2026-03-05T18:00:00.000Z,,WEIRD NEW THING,,,USD 1,USD,1.08
"""


def test_revolut_import_is_idempotent_and_classifies_rows():
    res = revolut.import_text(CSV, "P1")
    assert (res["trades"], res["cash"], res["duplicates"]) == (3, 3, 0)
    assert res["skipped"] == {"STOCK SPLIT": 1, "MERGER - STOCK": 1, "WEIRD NEW THING": 1}
    assert len(res["unknown"]) == 1 and "WEIRD" in res["unknown"][0]
    buy = db.query_one("SELECT * FROM trades WHERE symbol = 'AAA' AND side = 'BUY'")
    assert buy["fees"] == pytest.approx(0.5)               # total 100.50 − 10 × 10
    assert db.query_one("SELECT 1 FROM trades WHERE symbol = 'BRK-B'")
    again = revolut.import_text(CSV, "P1")
    assert (again["trades"], again["cash"], again["duplicates"]) == (0, 0, 6)


@pytest.mark.parametrize("raw,expected", [
    ("USD 62.54", (62.54, "USD")), ("$1,234.56", (1234.56, "USD")), ("-€0.50", (-0.5, "EUR")),
    ("USD -203.55", (-203.55, "USD")), ("12,5", (12.5, None)), ("", (None, None)),
])
def test_money_parsing(raw, expected):
    assert revolut._money(raw) == expected


GENERIC = """date,type,symbol,quantity,price,fees,amount,currency
2026-03-02,DEPOSIT,,,,,1000,USD
2026-03-02 15:30,BUY,AAA,10,10.00,1.00,,USD
2026-03-03,AL,BRK.B,1,400,,,USD
2026-03-04,SELL,AAA,4,12.00,0.50,,USD
2026-03-04,DIVIDEND,AAA,,,,-0.40,USD
2026-03-05,FEE,,,,,2,USD
2026-03-05,SPLIT,AAA,10,,,,USD
2026-03-05,BUY,CCC,,5,,,USD
"""


def test_generic_csv_import_detection_signs_and_idempotence():
    from borsa_radar import importers
    from borsa_radar.importers import generic
    assert generic.looks_like(GENERIC) and not generic.looks_like(CSV)          # Revolut statements keep their own parser
    res = importers.import_text(GENERIC, "P1")
    assert res["format"] == "csv" and res["trades"] == 3 and res["cash"] == 3
    assert res["symbols"] == {"AAA", "BRK-B"} and len(res["unknown"]) == 1 and len(res["errors"]) == 1   # SPLIT unknown; CCC lacks qty
    t = {r["symbol"] + r["side"]: r for r in db.query("SELECT * FROM trades")}
    assert t["AAABUY"]["fees"] == 1.0 and t["AAASELL"]["qty"] == 4 and t["BRK-BBUY"]["source"] == "csv"
    cash = {r["kind"]: r["amount"] for r in db.query("SELECT kind, amount FROM cash_flows")}
    assert cash == {"deposit": 1000.0, "dividend": 0.40, "fee": -2.0}            # the type decides the sign
    again = importers.import_text(GENERIC, "P1")
    assert again["trades"] == 0 and again["cash"] == 0 and again["duplicates"] == 6
    assert importers.import_text(CSV, "P1", dry_run=True)["format"] == "revolut"
    bad = generic.parse("when,what\n2026-01-01,BUY\n")
    assert bad[0] == [] and "date" in bad[1][0]
