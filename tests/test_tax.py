import pytest

from borsa_radar import db, portfolio, tax


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    monkeypatch.setattr(tax, "_schema_ok", False)
    monkeypatch.setattr(tax, "fx_refresh", lambda: {"ok": False})   # never hit the network in tests
    db.init()
    portfolio.invalidate()
    tax.invalidate()
    tax._ensure_schema()


def _fx(rates: dict[str, float], ccy: str = "USD"):
    with db.tx() as c:
        c.executemany("INSERT OR REPLACE INTO fx_ecb(day, ccy, rate) VALUES (?,?,?)", [(d, ccy, r) for d, r in rates.items()])
    tax.invalidate()


def _t(side, sym, qty, price, day, fees=0, asset=None):
    return portfolio.add_trade(side=side, symbol=sym, qty=qty, price=price, fees=fees, sub="P1",
                               ts=f"{day}T15:00:00Z", asset=asset)


def _rep(year):
    tax.invalidate()
    return tax.report(year, refresh_fx=False)


def test_fifo_partial_sells_with_fees():
    _fx({"2024-03-01": 1.0})
    _t("BUY", "AAA", 10, 10, "2024-03-04", fees=1)
    _t("BUY", "AAA", 10, 20, "2024-03-05")
    _t("SELL", "AAA", 15, 30, "2024-03-06", fees=2)
    _t("SELL", "AAA", 5, 40, "2024-03-07")
    r = _rep(2024)
    s1, s2 = r["sales"]
    assert s1["cost_eur"] == pytest.approx(101 + 5 * 20)       # first lot incl. its fee, then half of the second
    assert s1["proceeds_eur"] == pytest.approx(15 * 30 - 2)
    assert s1["gain_eur"] == pytest.approx(448 - 201)
    assert s1["lots"] == 2
    assert s2["cost_eur"] == pytest.approx(100)                 # remaining 5 @ 20
    assert s2["gain_eur"] == pytest.approx(100)
    assert r["pots"]["aktien"]["net"] == pytest.approx(347)
    assert r["open_lots"] == []


def test_eur_conversion_uses_each_days_rate():
    _fx({"2024-01-02": 1.25, "2024-06-03": 1.0})
    _t("BUY", "AAA", 10, 100, "2024-01-03")      # 1000 USD at 1.25 USD/EUR → 800 €
    _t("SELL", "AAA", 10, 100, "2024-06-04")     # 1000 USD at 1.00 → 1000 €
    s = _rep(2024)["sales"][0]
    assert s["gain_ccy"] == pytest.approx(0)
    assert s["cost_eur"] == pytest.approx(800)
    assert s["proceeds_eur"] == pytest.approx(1000)
    assert s["gain_eur"] == pytest.approx(200)
    assert s["sell_rate_day"] == "2024-06-03" and s["sell_rate_src"] == "ECB"


def test_missing_fx_is_unverified_not_guessed():
    _t("BUY", "AAA", 1, 100, "2024-01-03")
    _t("SELL", "AAA", 1, 110, "2024-01-04")
    r = _rep(2024)
    assert r["sales"][0]["gain_eur"] is None
    assert any("kur" in i["text"] for i in r["issues"])


def test_split_puts_quantities_on_one_basis():
    _fx({"2024-01-01": 1.0})
    with db.tx() as c:
        c.execute("INSERT INTO splits(symbol, day, ratio) VALUES ('SPL', '2024-06-10', 4.0)")
    _t("BUY", "SPL", 1, 400, "2024-06-03")       # pre-split: 1 share @ 400 → 4 @ 100
    _t("SELL", "SPL", 2, 120, "2024-06-20")      # post-split
    r = _rep(2024)
    s = r["sales"][0]
    assert s["qty"] == pytest.approx(2)
    assert s["cost_eur"] == pytest.approx(200)
    assert s["gain_eur"] == pytest.approx(40)
    assert r["open_lots"][0]["qty"] == pytest.approx(2)


def test_stock_losses_only_offset_stock_gains_and_carry_forward():
    _fx({"2023-01-02": 1.0})
    _t("BUY", "AAA", 10, 100, "2023-03-01")
    _t("SELL", "AAA", 10, 50, "2023-03-02")                      # −500 stock loss
    portfolio.add_cash(kind="dividend", amount=85, sub="P1", ts="2023-04-01T10:00:00Z", symbol="BBB", signed=True)
    r23 = _rep(2023)
    st = r23["steps"]
    assert r23["div"]["gross"] == pytest.approx(100)              # 85 net / 0.85
    assert r23["div"]["wht"] == pytest.approx(15)
    assert st["other_total"] == pytest.approx(100)
    assert st["base"] == pytest.approx(100)                       # stock loss does not touch the dividend
    assert st["taxable"] == pytest.approx(0)                      # under the Pauschbetrag
    assert st["carry_s_out"] == pytest.approx(500)
    _t("BUY", "CCC", 10, 100, "2024-02-01")
    _t("SELL", "CCC", 10, 280, "2024-02-02")                      # +1800 stock gain
    st = _rep(2024)["steps"]
    assert st["used_s"] == pytest.approx(500)
    assert st["taxable"] == pytest.approx(1800 - 500 - 1000)
    assert st["tax"] == pytest.approx(300 * 0.25 * 1.055)
    assert st["carry_s_out"] == pytest.approx(0)


def test_other_losses_offset_stock_gains():
    _fx({"2024-01-02": 1.0})
    _t("BUY", "AAA", 10, 100, "2024-03-01")
    _t("SELL", "AAA", 10, 400, "2024-03-02")                      # +3000 stock
    _t("BUY", "AAA240621C00100000", 1, 5, "2024-03-01")           # option: 1 × 5 × 100 = 500
    _t("SELL", "AAA240621C00100000", 1, 1, "2024-03-05")          # −400
    r = _rep(2024)
    assert r["pots"]["sonstige"]["net"] == pytest.approx(-400)
    st = r["steps"]
    assert st["other_offsets_stock"] == pytest.approx(400)
    assert st["taxable"] == pytest.approx(3000 - 400 - 1000)
    labels = [m["field"] for m in r["mapping"]]
    assert any("Termingeschäften" in x for x in labels)


def test_crypto_one_year_rule_and_freigrenze():
    _fx({"2023-01-02": 1.0})
    _t("BUY", "BTC-USD", 1, 20000, "2023-01-10")
    _t("BUY", "BTC-USD", 1, 30000, "2024-06-01")
    _t("SELL", "BTC-USD", 1.5, 40000, "2024-07-01")   # lot 1 (>1y, free) + half of lot 2 (≤1y)
    r = _rep(2024)
    pots = r["pots"]
    assert pots["krypto_frei"]["net"] == pytest.approx(20000)
    assert pots["krypto"]["net"] == pytest.approx(0.5 * 40000 - 15000)
    assert r["crypto"]["taxable"] == pytest.approx(5000)          # above the Freigrenze → fully taxable
    assert r["steps"]["taxable"] == 0                              # not Abgeltungsteuer
    assert any(m["form"] == "Anlage SO" for m in r["mapping"])


def test_crypto_below_freigrenze_is_tax_free():
    _fx({"2025-01-02": 1.0})
    _t("BUY", "ETH-USD", 1, 2000, "2025-02-01")
    _t("SELL", "ETH-USD", 1, 2900, "2025-03-01")                 # +900 < 1000
    c = _rep(2025)["crypto"]
    assert c["net"] == pytest.approx(900)
    assert c["freigrenze_applied"] and c["taxable"] == 0


def test_oversell_is_ignored_and_listed():
    _fx({"2024-01-02": 1.0})
    _t("BUY", "AAA", 5, 10, "2024-03-01")
    _t("SELL", "AAA", 8, 12, "2024-03-02")
    r = _rep(2024)
    s = r["sales"][0]
    assert s["qty"] == pytest.approx(5) and s["excess"] == pytest.approx(3)
    assert s["gain_eur"] == pytest.approx(10)
    assert any("fazla satış" in i["text"] for i in r["issues"])


def test_etf_teilfreistellung():
    _fx({"2024-01-02": 1.0})
    _t("BUY", "VOO", 10, 100, "2024-03-01", asset="etf")
    _t("SELL", "VOO", 10, 300, "2024-03-02", asset="etf")
    r = _rep(2024)
    assert r["pots"]["fonds"]["net"] == pytest.approx(2000)
    assert r["pots"]["fonds"]["taxable"] == pytest.approx(1400)
    assert r["steps"]["taxable"] == pytest.approx(400)
    assert any(m["form"] == "Anlage KAP-INV" for m in r["mapping"])


def test_ecb_parse_and_csv_and_brief():
    rows = tax.parse_ecb_csv("Date,USD,JPY,CYP,\n2024-01-03,1.0919,155.1,N/A,\n2024-01-02,1.0956,155.6,N/A,\n", {"USD"})
    assert rows == [("2024-01-03", "USD", 1.0919), ("2024-01-02", "USD", 1.0956)]
    _fx({"2024-01-02": 1.1})
    _t("BUY", "AAA", 2, 55, "2024-03-01")
    _t("SELL", "AAA", 2, 66, "2024-03-02")
    out = tax.csv(2024)
    assert "satis;AAA" in out and "20,00" in out                 # (132 − 110) / 1.1 = 20 €
    b = tax.brief(_rep(2024))
    assert b["stock_pot_net_eur"] == pytest.approx(20) and b["est_tax_eur"] == 0
    assert "danışmanlığı değildir" in b["disclaimer"]
