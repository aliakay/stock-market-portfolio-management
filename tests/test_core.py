from datetime import datetime, timezone

import pytest

from borsa_radar import db, llm
from borsa_radar.collectors import news, ratings
from borsa_radar.util import in_quiet_hours, jaccard, normalize_url, title_tokens


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()


def test_quiet_hours_wraps_midnight():
    # 23:30 UTC = 00:30 Berlin (winter)
    assert in_quiet_hours(datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc), "00:00", "08:00")
    assert not in_quiet_hours(datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc), "00:00", "08:00")
    assert in_quiet_hours(datetime(2026, 11, 7, 22, 30, tzinfo=timezone.utc), "23:00", "07:00")


def test_url_normalization_drops_tracking():
    a = normalize_url("https://Example.com/a/?utm_source=x&id=3")
    assert a == "https://example.com/a?id=3"


def test_near_duplicate_titles():
    a = title_tokens("Nvidia stock jumps after earnings beat - Yahoo Finance")
    b = title_tokens("Nvidia stock jumps after earnings beat")
    assert jaccard(a, b) == 1.0


def test_mentions_short_ticker_needs_context():
    t = {"symbol": "MU", "short_name": "Micron Technology, Inc.", "name": "Micron Technology, Inc."}
    assert news.mentions(t, "Micron raises guidance")
    assert news.mentions(t, "Shares of $MU rallied")
    assert not news.mentions(t, "MU Online game update")  # bare 2-letter ticker is ignored
    assert news.keywords(t)[0] == "Micron"


def test_rating_classification():
    assert ratings.classify("up", "Buy", 100, 120, 10)[:2] == (4, 1)
    assert ratings.classify("down", "Hold", None, None, 10)[:2] == (4, -1)
    assert ratings.classify("init", "Overweight", None, 50, 10)[:2] == (3, 1)
    imp, d, chg = ratings.classify("main", "Buy", 420, 315, 10)
    assert (imp, d) == (3, -1) and round(chg) == -25
    assert ratings.classify("reit", "Buy", 300, 300, 10)[:2] == (1, 0)


def test_insert_event_dedupes_and_merges_symbols():
    kw = dict(type="news", source="x", title="t", published_at="2026-11-07T00:00:00+00:00", dedupe_key="k", status="pending")
    eid = db.insert_event(symbol="NVDA", **kw)
    assert eid
    assert db.insert_event(symbol="MU", **kw) is None
    assert db.event_symbols(eid) == ["MU", "NVDA"]


def test_parse_json_variants():
    assert llm.parse_json('```json\n[{"id": 1}]\n```') == [{"id": 1}]
    assert llm.parse_json('Here: {"a": 2} thanks') == {"a": 2}
    with pytest.raises(llm.LLMError):
        llm.parse_json("no json")


def test_parse_json_object_with_inner_list():
    txt = '{"findings_tr": "x", "sources": [{"title": "a", "url": "https://a"}]}'
    assert llm.parse_json(txt)["findings_tr"] == "x"
    assert llm.parse_json('[{"id": 1, "tags": ["a"]}]') == [{"id": 1, "tags": ["a"]}]


def test_kalshi_implied_median():
    from borsa_radar.collectors import kalshi
    for lab, p in [("Above 3.0%", 0.9), ("Above 3.2%", 0.6), ("Above 3.4%", 0.3), ("Above 3.6%", 0.1)]:
        db.execute("INSERT INTO kalshi_markets(market_ticker, event_ticker, series, label, prob, updated_at) VALUES (?,?,?,?,?,?)",
                   (lab, "EV", "KXCPIYOY", lab, p, "2026-11-07T00:00:00+00:00"))
    assert kalshi.implied_median("EV")[1] == 3.267


def test_usaspending_strict_name_match():
    import re
    from borsa_radar.collectors import news as n
    t = {"symbol": "EOSE", "short_name": "Eos Energy Enterprises, Inc.", "name": "Eos Energy Enterprises, Inc."}
    kw = n.keywords(t)[0].upper()
    pat = re.compile(rf"^{re.escape(kw)}\b")
    assert pat.search("EOS ENERGY STORAGE LLC") and not pat.search("AEOS ENERGY, LLC.")


def test_research_clean_markdown_links():
    from borsa_radar import research
    assert research.clean("x ([bls.gov](https://bls.gov/a?utm_source=openai)) y") == "x (bls.gov) y"
    assert research._clean_url("https://a.com/x?utm_source=openai") == "https://a.com/x"
