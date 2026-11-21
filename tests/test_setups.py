from datetime import date, timedelta

import pytest

from borsa_radar import setups
from borsa_radar.setups import Arr, Setup, follow


def bars_from(closes, start=date(2025, 1, 1), spread=0.01, vols=None):
    out, d = [], start
    for k, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"day": d.isoformat(), "open": c, "high": c * (1 + spread), "low": c * (1 - spread), "close": c,
                    "volume": (vols[k] if vols else 1_000_000)})
        d += timedelta(days=1)
    return out


ROW = {"day": "2025-01-01", "valid_until": "2025-01-08", "entry_lo": 100.0, "entry_hi": 101.0, "stop": 95.0, "t1": 110.0, "t2": 120.0}


def mk(day, o, h, lo, c):
    return {"day": day, "open": o, "high": h, "low": lo, "close": c}


def test_follow_expired_without_trigger():
    bars = [mk(f"2025-01-0{d}", 98, 99, 97, 98) for d in range(2, 10)]
    assert follow(ROW, bars)["status"] == "expired"


def test_follow_invalid_before_trigger():
    st = follow(ROW, [mk("2025-01-02", 97, 98, 94, 94.5)])
    assert st["status"] == "invalid"


def test_follow_trigger_then_stop():
    st = follow(ROW, [mk("2025-01-02", 99, 101, 99, 100.5), mk("2025-01-03", 99, 100, 94, 96)])
    assert st["status"] == "stop" and st["trig_px"] == 100.5
    assert st["r_mult"] == pytest.approx((95 - 100.5) / (100.5 - 95))   # -1R


def test_follow_t1_then_breakeven_and_t2():
    b = [mk("2025-01-02", 99, 101, 99, 100), mk("2025-01-03", 100, 111, 100, 109), mk("2025-01-06", 109, 121, 108, 120)]
    st = follow(ROW, b)
    assert st["status"] == "t2" and st["t1_day"] == "2025-01-03"
    assert st["r_mult"] == pytest.approx(0.5 * 10 / 5 + 0.5 * 20 / 5)
    b2 = b[:2] + [mk("2025-01-06", 105, 106, 99, 99.5)]          # back to entry after T1 → breakeven on half
    st2 = follow(ROW, b2)
    assert st2["status"] == "t1" and st2["exit_day"] == "2025-01-06"
    assert st2["r_mult"] == pytest.approx(0.5 * 10 / 5)


def test_follow_same_bar_stop_wins_and_gap_fill():
    b = [mk("2025-01-02", 99, 101, 99, 100), mk("2025-01-03", 100, 112, 94, 105)]
    assert follow(ROW, b)["status"] == "stop"
    g = [mk("2025-01-02", 99, 101, 99, 100), mk("2025-01-03", 90, 91, 89, 90)]
    st = follow(ROW, g)
    assert st["exit_px"] == 90  # gapped through the stop: fill at the open


def test_weighted_rr():
    s = Setup("X", "pullback", "2025-01-01", 100, 100, 95, 110, 120, "", "", "", "", "", "", "", "")
    assert s.rr == pytest.approx((0.5 * 10 + 0.5 * 20) / 5)


def test_indicator_arrays():
    a = Arr(bars_from([float(x) for x in range(1, 260)]))
    assert a.sma50[-1] == pytest.approx(sum(range(210, 260)) / 50)
    assert a.rsi[-1] == 100.0
    assert a.atr[-1] > 0 and a.ema20[-1] > a.ema50[-1]


def test_pullback_detected_in_uptrend():
    up = [100 * (1.004 ** k) for k in range(240)]
    top = up[-1]
    pull = [top * (1 - 0.009 * k) for k in range(1, 6)]          # ~4.5% dip on light volume
    closes = up + pull
    vols = [1_000_000] * 240 + [600_000] * 5
    a = Arr(bars_from(closes, vols=vols))
    s, why = setups.det_pullback("TEST", a, a.n - 1)
    assert s is not None, why
    assert s.stop < s.entry_lo < s.t1 < s.t2
    assert "Ocak" in s.trigger or "Şubat" in s.trigger or "Kasım" in s.trigger or "'" in s.trigger


def test_downtrend_rejected():
    closes = [200 * (0.997 ** k) for k in range(260)]
    a = Arr(bars_from(closes, vols=[2_000_000] * 260))
    found, why = setups.scan_symbol("DOWN", a, a.n - 1, None, None, None)
    assert found == [] and why.startswith("düşüş trendi")


def test_turkish_suffixes():
    assert setups.tr_de("2026-11-04") == "4 Kasım'da"
    assert setups.tr_de("2026-09-04") == "4 Eylül'de"
    assert setups.tr_den("2026-04-30") == "30 Nisan'dan"
    assert setups.tr_money(1234.5) == "1.234,50 $"


def test_status_alert_message(monkeypatch):
    sent = []
    monkeypatch.setattr(setups.db, "get_setting", lambda k: "1")
    from borsa_radar import notifier
    monkeypatch.setattr(notifier, "enqueue", lambda text, **kw: sent.append(text))
    row = dict(ROW, symbol="MSFT", kind="pullback", status="waiting", source="live")
    setups._status_alert(row, {"status": "triggered", "trig_px": 100.5, "exit_px": None, "r_mult": None})
    setups._status_alert(row, {"status": "stop", "trig_px": 100.5, "exit_px": 95.0, "r_mult": -1.0})
    setups._status_alert(row, {"status": "expired", "trig_px": None, "exit_px": None, "r_mult": None})
    assert len(sent) == 2
    assert "MSFT" in sent[0] and "Geri Çekilme" in sent[0] and "TETİKLENDİ" in sent[0] and "100,50" in sent[0]
    assert "Stop" in sent[1] and "-1" in sent[1] and "/h/MSFT" in sent[1]


def test_context_text_from_snapshot():
    from borsa_radar import context
    snap = {"portfolio": {"as_of": "2026-11-13", "value": 1000, "periods": {"day": {"pct": -1.0, "pnl": -10, "spx": -0.4}},
                          "positions": [{"symbol": "NVDA", "weight_pct": 50.0, "day_pct": -2.0, "unreal_pct": 3.0, "karne": 59}]},
            "regime": {"day": "2026-11-13", "label": "Temkinli Boğa", "score": 64, "summary": None},
            "setups": {"day": "2026-11-13", "today": [], "open": []}}
    t = context.text(snap)
    assert "2026-11-13" in t and "NVDA" in t and "karne 59" in t and "Temkinli Boğa 64/100" in t


# ── discovery tier (wide index universe) ──

def _row(i, q, sector="Tech", atr=2.0, entry=100.0, stop=96.0, rr=2.0, sym=None):
    return {"id": i, "symbol": sym or f"S{i}", "quality": q, "sector": sector, "ind": {"atr": atr}, "entry": entry, "stop": stop, "rr": rr}


def test_pick_discovery_strongest_first_with_sector_cap_and_threshold():
    rows = [_row(1, 96), _row(2, 95), _row(3, 94), _row(4, 93, "Health"), _row(5, 92, "Energy"), _row(6, 91, "Energy"),
            _row(7, 90, "Energy"), _row(8, 89, "Utilities"), _row(9, 60, "Materials")]
    assert setups.pick_discovery(rows) == [1, 2, 4, 5, 6]          # third Tech name skipped, then the cap of five
    assert setups.pick_discovery([_row(1, setups.DISCOVERY_MIN_QUALITY - 1)]) == []
    assert setups.pick_discovery([]) == []


def test_pick_discovery_skips_stops_inside_one_atr_and_is_deterministic():
    tight = _row(1, 99, stop=99.0)                                  # risk 1.0 < 1 × ATR 2.0
    ok = _row(2, 90)
    assert setups.pick_discovery([tight, ok]) == [2]
    a, b = _row(1, 90, sym="BBB"), _row(2, 90, "Health", sym="AAA")
    assert setups.pick_discovery([a, b]) == setups.pick_discovery([b, a]) == [2, 1]   # ties: by symbol


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    from borsa_radar import db
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    db.init()
    return db


def test_universe_tiers_own_list_wins(tmpdb, monkeypatch):
    from borsa_radar import breadth, portfolio, stockdata
    monkeypatch.setattr(portfolio, "positions", lambda: [{"symbol": "NVDA", "asset": "stock", "sub": "P1"}])
    tmpdb.set_setting("setup_extra", "MSFT")
    tmpdb.set_setting("setup_wide", "sp500 nope")
    members = [{"symbol": "NVDA", "name": "Nvidia", "sector": "Information Technology"}, {"symbol": "MAS", "name": "Masco", "sector": "Industrials"}]
    monkeypatch.setattr(breadth, "index_members", lambda code: stockdata.Fetched(members, "2026-11-16T00:00:00+00:00", "test"))
    assert [u["symbol"] for u in setups.universe()] == ["NVDA", "MSFT"]                 # default: own list only (other callers rely on it)
    wide = {u["symbol"]: u for u in setups.universe(wide=True)}
    assert wide["NVDA"]["tier"] == "liste" and wide["NVDA"]["held"] and wide["MSFT"]["src"] == "havuz"
    assert wide["MAS"] == {"symbol": "MAS", "src": "S&P 500", "held": False, "tier": "kesif", "company": "Masco", "sector": "Industrials"}
    assert setups.wide_lists() == ["sp500"]                                              # unknown list codes are ignored
    tmpdb.set_setting("setup_wide", "")
    assert "MAS" not in {u["symbol"] for u in setups.universe(wide=True)}


def _setup(sym):
    return Setup(sym, "pullback", "2026-11-13", 100.0, 101.0, 96.0, 108.0, 115.0, "e", "s", "t1", "t2", "durum", "tetik", "playbook", "yani",
                 quality=90.0, ind={"atr": 2.0, "px": 100.0})


def test_discovery_rows_stay_out_of_pages_and_alerts_unless_picked(tmpdb, monkeypatch):
    own = setups.store(_setup("MSFT"), {"symbol": "MSFT", "src": "havuz", "tier": "liste"}, source="live", regime=None)
    d1 = setups.store(_setup("MAS"), {"symbol": "MAS", "src": "S&P 500", "tier": "kesif", "company": "Masco", "sector": "Industrials"}, source="live", regime=None)
    d2 = setups.store(_setup("BBY"), {"symbol": "BBY", "src": "S&P 500", "tier": "kesif", "company": "Best Buy", "sector": "Consumer Discretionary"},
                      source="live", regime=None)
    monkeypatch.setattr(setups, "DISCOVERY_CARDS", 1)
    monkeypatch.setattr(setups, "_earnings_inside", lambda sym, day, until: "Bilanço uyarısı" if sym == "BBY" else None)
    picked = setups.mark_discovery("2026-11-13")
    assert picked == [d2]                                                                # same quality and R:R: by symbol
    assert [c["id"] for c in setups.for_day("2026-11-13")] == [own]                      # own cards never mix with discovery
    disc = setups.discovery_for_day("2026-11-13")
    assert [c["id"] for c in disc] == [d2, d1] and disc[0]["company"] == "Best Buy" and disc[0]["earn_warn"] == "Bilanço uyarısı"
    assert {c["id"] for c in setups.open_setups()} == {own, d2}                          # the unpicked one is invisible elsewhere
    sent = []
    monkeypatch.setattr(setups, "_status_alert", lambda r, st: sent.append(r["symbol"]))
    monkeypatch.setattr(setups.prices, "bars", lambda sym, start=None: [mk("2026-11-16", 100.5, 102, 100, 101.5)])
    setups.update_outcomes("live")                                                       # all three trigger on the next bar
    assert sent == ["MSFT"]                                                              # Telegram only for the own list
    assert tmpdb.query_one("SELECT status FROM setups WHERE id = ?", (d1,))["status"] == "triggered"   # still followed for the record


def test_discovery_record_replays_the_pick_rule(tmpdb, monkeypatch):
    monkeypatch.setattr(setups, "DISCOVERY_CARDS", 1)
    wide = {"src": "S&P 500", "tier": "kesif", "sector": "Industrials"}
    a = setups.store(_setup("AAA"), {"symbol": "AAA", **wide}, source="backtest", regime=None)      # picked (ties: by symbol)
    b = setups.store(_setup("BBB"), {"symbol": "BBB", **wide}, source="backtest", regime=None)
    tmpdb.execute("UPDATE setups SET status = 'stop', r_mult = -1.0, exit_day = '2026-11-18' WHERE id = ?", (a,))
    tmpdb.execute("UPDATE setups SET status = 't2', r_mult = 2.0, t1_day = '2026-11-17', exit_day = '2026-11-19' WHERE id = ?", (b,))
    rec = setups.discovery_record(save=True)
    assert rec["picks"]["n"] == 1 and rec["picks"]["exp_r"] == -1.0 and rec["picks"]["hit"] == 0.0
    assert rec["all"]["n"] == 2 and rec["all"]["exp_r"] == 0.5 and rec["all"]["hit"] == 50.0
    assert setups.report("2026-11-13")["discovery_record"]["picks"]["n"] == 1                       # stored for the page
    tmpdb.execute("DELETE FROM setups")
    assert setups.discovery_record() is None
