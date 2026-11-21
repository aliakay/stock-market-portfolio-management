from datetime import timedelta

import pytest

from borsa_radar import db, notifier
from borsa_radar.util import iso, utcnow, utcnow_iso


@pytest.fixture(autouse=True)
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    db.init()
    monkeypatch.setattr(notifier, "_held", lambda: {"NVDA"})
    monkeypatch.setattr(notifier, "_price_line", lambda syms: None)
    db.execute("INSERT OR IGNORE INTO tickers(symbol, name, active, added_at) VALUES ('NVDA','NVIDIA',1,?)", (utcnow_iso(),))
    db.execute("INSERT OR IGNORE INTO tickers(symbol, name, active, added_at) VALUES ('MU','Micron',1,?)", (utcnow_iso(),))


_n = iter(range(10_000))


def _ev(title, *, sym="NVDA", imp=4, typ="news", story=None, new_info=1, summary=None, direction=-1):
    eid = db.insert_event(type=typ, source="Reuters", title=title, published_at=iso(utcnow() - timedelta(minutes=5)),
                          dedupe_key=f"k{next(_n)}", status="analyzed", symbol=sym, symbols=[sym], importance=imp, direction=direction)
    db.execute("""INSERT INTO analyses(event_id, summary_tr, reasoning_tr, category, direction, importance, model, created_at, story, new_info)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""", (eid, summary or f"Özet: {title}", "Neden önemli.", "management", direction, imp,
                                                    "test", utcnow_iso(), story, new_info))
    return eid


def _sel():
    notifier.select_alerts()
    return {r["event_id"]: (r["status"], r["reason"]) for r in db.query("SELECT * FROM notifications WHERE kind='alert'")}


def test_same_story_pushes_once_and_updates_need_new_facts():
    a = _ev("Huang resigns as Nvidia CEO", imp=5, story="nvda-ceo-huang-exit")
    b = _ev("How Huang's resignation unfolded", imp=5, story="nvda-ceo-huang-exit", new_info=0)
    c = _ev("Nvidia board names interim CEO", imp=5, story="nvda-ceo-huang-exit", new_info=1)
    d = _ev("Analysts react to Huang exit", imp=4, story="nvda-ceo-huang-exit", new_info=1)
    r = _sel()
    assert r[a] == ("queued", "new_story")
    assert r[b] == ("dropped", "same_story")
    assert r[c] == ("queued", "story_update")
    assert r[d] == ("dropped", "same_story_lower")


def test_story_cap():
    ids = [_ev(f"Update {i}", imp=5, story="s", new_info=1) for i in range(5)]
    r = _sel()
    assert [r[i][0] for i in ids] == ["queued"] * 3 + ["dropped"] * 2


def test_threshold_flow_and_watch_only():
    low = _ev("Minor note", imp=3)
    flow = _ev("NVDA short interest +40%", imp=4, typ="flow")
    watch = _ev("Micron update", sym="MU", imp=4)          # not held → needs 5
    watch5 = _ev("Micron guidance shock", sym="MU", imp=5)
    r = _sel()
    assert r[low] == ("dropped", "below_threshold")
    assert r[flow] == ("dropped", "no_push_type")
    assert r[watch] == ("dropped", "below_threshold")
    assert r[watch5][0] == "queued"


def test_similar_title_without_story():
    a = _ev("US plans to name Nvidia among AI national champions, WSJ reports")
    b = _ev("Trump administration to name Nvidia among AI national champions")
    r = _sel()
    assert r[a][0] == "queued" and r[b] == ("dropped", "similar_title")


def test_flush_groups_alerts_and_replies_research(monkeypatch):
    sent = []

    def fake_send(text, reply_to=None):
        sent.append((text, reply_to))
        return True, None, 100 + len(sent)

    monkeypatch.setattr(notifier, "send_telegram", fake_send)
    monkeypatch.setattr(notifier, "in_quiet_hours", lambda **k: False)
    db.set_setting("telegram_enabled", "1")
    a = _ev("Nvidia news one", imp=5, story="one")
    _ev("Micron shock", sym="MU", imp=5, story="two")
    notifier.select_alerts()
    notifier.enqueue("🔔 price alert", kind="price_alert")
    notifier.flush()
    assert sent[0][0] == "🔔 price alert"
    assert sent[1][0].startswith("📰 <b>2 yeni gelişme</b>") and "<b>NVDA</b>" in sent[1][0] and "<b>MU</b>" in sent[1][0]
    assert len(sent) == 2
    notifier.enqueue("🔍 deep look", kind="research", event_id=a)
    notifier.flush()
    assert sent[2] == ("🔍 deep look", 102)  # reply under the grouped alert message


def test_single_alert_format_is_turkish_first():
    e = db.get_event(_ev("English headline", imp=5, story="x", summary="Türkçe özet cümlesi."))
    t = notifier.format_event(e)
    first, second = t.split("\n")[:2]
    assert "NVDA" in first and "Yönetim" in first and "5/5" in first
    assert second == "<b>Türkçe özet cümlesi.</b>" and "English headline" not in t
