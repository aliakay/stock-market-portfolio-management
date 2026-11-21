from datetime import datetime, timedelta

import pytest

from borsa_radar import analyzer, config, db, llm
from borsa_radar.util import iso, utcnow


@pytest.fixture(autouse=True)
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    db.init()
    monkeypatch.setattr(analyzer, "_held", lambda: {"NVDA"})


def _at(h, m=0):
    return datetime(2026, 11, 16, h, m, tzinfo=config.LOCAL_TZ)


def test_paced_allowance_spreads_budget_toward_us_hours():
    vals = [llm.paced_allowance(_at(h, 59)) for h in range(24)]
    assert vals == sorted(vals)
    budget = db.get_int("llm_daily_cap") - llm.RESERVE
    assert vals[0] >= llm.BURST and vals[-1] >= budget - 1
    # most of the budget is released during 14:00-23:00 Berlin
    assert vals[22] - vals[13] > (budget - llm.BURST) / 2
    assert llm.paced_allowance(_at(10)) < budget / 2


def test_budget_tiers(monkeypatch):
    cap = db.get_int("llm_daily_cap")
    monkeypatch.setattr(llm, "paced_allowance", lambda now=None: 50)
    monkeypatch.setattr(llm, "usage_today", lambda: 60)
    assert llm.budget_left("paced") == -10
    assert llm.budget_left("held") == 50 + llm.AHEAD - 60
    assert llm.budget_left("daily") == cap - llm.RESERVE - 60
    assert llm.budget_left("priority") == cap - 60
    with pytest.raises(llm.CapReached):
        llm.complete("x")


def _ev(title, *, sym=None, typ="news", source="Yahoo", age_min=0, published_h=1, imp=None):
    eid = db.insert_event(type=typ, source=source, title=title, published_at=iso(utcnow() - timedelta(hours=published_h)),
                          dedupe_key=title + str(age_min) + str(published_h), status="pending", symbols=[sym] if sym else [],
                          importance=imp)
    db.execute("UPDATE events SET created_at = ? WHERE id = ?", (iso(utcnow() - timedelta(minutes=age_min)), eid))
    return eid


def _status(eid):
    return db.query_one("SELECT status FROM events WHERE id = ?", (eid,))["status"]


def test_prefilter_drops_option_pages_boilerplate_duplicates_and_expires_old():
    opt = _ev("SOFI Dec 2026 18.000 put (SOFI261204P00018000) Interactive Stock Chart", sym="SOFI")
    std = _ev("(EVO) Trading Strategy and Analysis", source="Stock Traders Daily")
    first = _ev("Nvidia beats estimates", sym="NVDA")
    dup = _ev("NVIDIA beats estimates!", sym="NVDA", age_min=1)
    old = _ev("Old story", published_h=30)
    keep = _ev("Micron raises guidance", sym="MU")
    analyzer.prefilter()
    assert _status(opt) == "irrelevant" and _status(std) == "irrelevant"
    assert _status(first) == "pending" and _status(dup) == "duplicate"
    assert _status(old) == "backfill" and _status(keep) == "pending"


def test_queue_batches_and_orders_portfolio_first():
    fresh_other = [_ev(f"Other story {i}", sym="MU") for i in range(3)]
    assert analyzer._queue() == []  # small, fresh, non-portfolio batch waits
    held = _ev("Nvidia news", sym="NVDA", age_min=analyzer.HELD_WAIT_MIN + 1)
    q = analyzer._queue()
    assert [(m, b) for m, _, b in q] == [(False, "held")] and q[0][1][0]["id"] == held
    for i in range(analyzer.BATCH):
        _ev(f"Bulk {i}", sym="MU")
    q = analyzer._queue()
    assert [b for _, _, b in q] == ["held", "paced"] and len(q[1][1]) == analyzer.BATCH
    hot = _ev("Fed surprise", typ="macro", imp=4)
    assert any(m and b[0]["id"] == hot for m, b, _ in analyzer._queue())
    assert fresh_other


def test_cap_reached_keeps_items_pending(monkeypatch):
    eid = _ev("Nvidia news", sym="NVDA", age_min=30)
    monkeypatch.setattr(analyzer, "_run_batch", lambda *a, **k: (_ for _ in ()).throw(llm.CapReached("cap")))
    for _ in range(5):
        analyzer.analyze_pending()
    assert _status(eid) == "pending"
    assert db.query_one("SELECT json_extract(extra,'$.llm_fail') n FROM events WHERE id = ?", (eid,))["n"] is None


def test_refused_route_is_refunded_and_sits_out(monkeypatch):
    """A provider that reports its own quota costs nothing against the cap and is skipped for a while."""
    calls = []

    def fake_run(prompt, provider, model, timeout, toolsets="clarify"):
        calls.append(provider)
        if provider == "a":
            raise llm.LLMError("hermes exit=2: ⚠️ Codex provider quota exhausted")
        return "ok"
    monkeypatch.setattr(llm, "_run", fake_run)
    routes = [("a", "m1"), ("b", "m2")]
    assert llm.complete("x", priority=True, routes=routes) == ("ok", "b:m2")
    assert calls == ["a", "b"] and llm.usage_today() == 1          # the refused attempt was refunded
    assert llm.cooling("a", "m1") and not llm.cooling("b", "m2")
    assert llm.complete("x", priority=True, routes=routes) == ("ok", "b:m2")
    assert calls == ["a", "b", "b"] and llm.usage_today() == 2     # the cooling route is not tried again
    # every route cooling: still try rather than fail without a call
    with pytest.raises(llm.LLMError):
        llm.complete("x", priority=True, routes=[("a", "m1")])
    assert calls[-1] == "a" and llm.usage_today() == 2


def test_other_failures_still_count(monkeypatch):
    def fake_run(prompt, provider, model, timeout, toolsets="clarify"):
        raise llm.LLMError("hermes exit=1: connection reset")
    monkeypatch.setattr(llm, "_run", fake_run)
    with pytest.raises(llm.LLMError):
        llm.complete("x", priority=True, routes=[("a", "m1")])
    assert llm.usage_today() == 1 and not llm.cooling("a", "m1")


def test_cooldown_follows_the_providers_retry_after(monkeypatch):
    from borsa_radar.util import parse_dt

    def fake_run(prompt, provider, model, timeout, toolsets="clarify"):
        raise llm.LLMError("hermes exit=2: ⚠️ Codex provider quota exhausted (429); retry after 1888640s. Credentials are still valid.")
    monkeypatch.setattr(llm, "_run", fake_run)
    with pytest.raises(llm.LLMError):
        llm.complete("x", priority=True, routes=[("a", "m1")])
    left = (parse_dt(db.get_cursor(llm._cool_key("a", "m1"))) - utcnow()).total_seconds() / 60
    assert llm.COOLDOWN_MAX_MIN - 2 < left <= llm.COOLDOWN_MAX_MIN      # three weeks asked: capped at a day, far beyond the default hour
