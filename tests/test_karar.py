"""Verdict engine: pure tests on decide / board / comments, plus a storage round trip on a temp DB (no network, no model)."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from borsa_radar import db, karar, llm

TODAY = "2026-11-16"


def facts(**kw) -> dict:
    f = {"symbol": "XYZ", "today": TODAY,
         "tech": {"as_of": "2026-11-13", "price": 110.0, "sma50": 100.0, "sma200": 90.0, "above50": True, "above200": True, "trend_score": 80.0,
                  "trend_label": "Güçlü yükseliş trendi", "rs63_vs_spy": 5.0, "from_hi52": -5.0, "hi52": 115.0, "chg_63d": 10.0, "rsi14": 55.0},
         "card": {"as_of": "2026-11-13", "created_at": "2026-11-15T20:00:00+00:00", "overall": 70.0,
                  "dims": {"temel": {"score": 80.0, "parts": [{"label": "Gelir büyümesi (yıllık, son çeyrek)", "value": "%+20,0", "pts": 80}]},
                           "haber": {"score": 55.0, "parts": []}, "anomali": {"score": 70.0, "parts": []}},
                  "fair": {"fair": 150.0, "upside": 36.0, "confidence": "orta"}},
         "position": {"weight": 10.0, "unreal_pct": 5.0, "value": 1000.0, "avg": 100.0, "risk_share": 10.0, "beta": 1.1, "cluster": None},
         "thesis": None, "news": {"events": []},
         "regime": {"day": "2026-11-13", "label": "Temkinli Boğa", "score": 64.0, "tone": "pos", "weakest": None}, "earnings": []}
    for k, v in kw.items():
        f[k] = v
    return f


def broken(f: dict, price: float = 80.0) -> dict:
    f["tech"].update(price=price, above50=price > f["tech"]["sma50"], above200=price > f["tech"]["sma200"], trend_score=10.0)
    return f


def temel(f: dict, score: float) -> dict:
    f["card"]["dims"]["temel"]["score"] = score
    return f


def thesis(*rules) -> dict:
    return {"id": 7, "status": "active", "next_review": "2026-12-20", "review_due": False, "rules": list(rules)}


def rule(kind="price_below", param="70", status="ok", good=False, label="Kapanış 70$ altına inerse"):
    return {"id": 1, "kind": kind, "param": param, "label": label, "status": status, "last_value": "80,00$ (2026-11-13)", "good": good}


def keys(card, side=("for", "against", "notes")):
    return {s["key"] for k in side for s in card[k]}


# ── stances ──

def test_saglam_baseline():
    c = karar.decide(facts())
    assert c["stance"] == "saglam" and c["stance_tr"] == "Sağlam" and c["tone"] == "pos" and c["rank"] == 0
    assert c["pattern"] == "ikisi_guclu" and c["headline"].startswith("XYZ:")
    assert "tez_yok" in keys(c, ("notes",)) and c["todo"][0]["href"] == "/tez?symbol=XYZ#yeni"
    for side in ("for", "against", "notes"):
        assert [s["w"] for s in c[side]] == sorted((s["w"] for s in c[side]), reverse=True)
        assert all(set(s) == {"key", "text", "w", "src"} and 1 <= s["w"] <= 3 for s in c[side])
    assert c["as_of"] == "2026-11-13" and c["price"] == 110.0 and c["rules_ver"] == karar.RULES_VER


def test_izle_when_axes_conflict():
    c = karar.decide(broken(facts()))
    assert c["stance"] == "izle" and c["pattern"] == "sirket_guclu_fiyat_zayif" and c["pattern_tr"] == "Sağlam şirket, bozuk grafik"
    assert "fiyat_bozuk" in keys(c, ("against",)) and "temel_guclu" in keys(c, ("for",))


def test_sorgula_both_weak_and_deep_loss_without_exit_rule():
    c = karar.decide(broken(temel(facts(), 30.0)))
    assert c["stance"] == "sorgula" and c["pattern"] == "ikisi_zayif"
    f = broken(temel(facts(), 55.0))
    f["position"]["unreal_pct"] = -30.0
    c = karar.decide(f)
    assert c["stance"] == "sorgula" and "derin_zarar" in keys(c, ("against",))
    f["thesis"] = thesis(rule())          # written exit rule (not triggered) → no longer "sorgula"
    assert karar.decide(f)["stance"] == "izle"


def test_triggered_exit_rule_always_wins():
    f = facts(thesis=thesis(rule(status="triggered")))
    c = karar.decide(f)
    assert c["stance"] == "alarm" and c["rank"] == 3 and c["todo"][0]["href"] == "/tez#t7"
    assert "kural_ihlali" in keys(c, ("against",))
    good = facts(thesis=thesis(rule(kind="price_above", param="105", status="triggered", good=True, label="Kapanış 105$ üstüne çıkarsa (hedef)"),
                               rule()))
    assert karar.decide(good)["stance"] == "saglam"


def test_veri_yok_without_card_or_prices():
    assert karar.decide(facts(card=None))["stance"] == "veri_yok"
    c = karar.decide(facts(tech=None))
    assert c["stance"] == "veri_yok" and c["rank"] == -1 and c["as_of"] is None and c["changers"]["up"]


def test_missing_temel_is_never_saglam():
    f = facts()
    f["card"]["dims"]["temel"]["score"] = None
    c = karar.decide(f)
    assert c["stance"] == "izle" and c["pattern"].startswith("sirket_bilinmiyor")


def test_red_flags_block_saglam():
    f = facts()
    f["card"]["dims"]["anomali"]["parts"] = [{"label": "Hisse sayısı artışı (sulandırma, yıllık)", "value": "%+6,0", "pts": -15}]
    c = karar.decide(f)
    assert c["stance"] == "izle" and any("sulandırma" in u["text"] or "%3" in u["text"] for u in c["changers"]["up"])
    f = facts(regime={"day": "2026-11-13", "label": "Risk-off", "score": 40.0, "tone": "neg"})
    f["position"]["beta"] = 2.0
    assert karar.decide(f)["stance"] == "izle"
    f["position"]["beta"] = 1.0
    assert karar.decide(f)["stance"] == "saglam"


# ── known bad inputs ──

def test_low_confidence_fair_value_never_drives_a_signal():
    f = facts()
    f["card"]["fair"] = {"fair": 586.0, "upside": 260.0, "confidence": "düşük"}
    c = karar.decide(f)
    ks = keys(c)
    assert "degerleme_disarida" in ks and not ks & {"degerleme_ucuz", "degerleme_pahali"}
    assert all("260" not in s["text"] for k in ("for", "against", "notes") for s in c[k])
    assert "degerleme_ucuz" in keys(karar.decide(facts()))      # medium confidence → used


def test_duplicate_stories_counted_once():
    neg = [{"story": "s:ceo-exit", "dir": -1, "imp": 4, "title": f"Outlet {i}: totally different words {i}", "at": "2026-11-14"} for i in range(6)]
    t = karar.news_tone(neg + [{"story": "s:deal", "dir": 1, "imp": 3, "title": "Deal", "at": "2026-11-13"}])
    assert t["n"] == 2 and t["events"] == 7 and t["by_story"]
    assert t["net"] == pytest.approx((-1 * 4 + 1 * 3) / 7)
    c = karar.decide(facts(news={"events": neg}))       # one story, many outlets → below the minimum, no tone signal
    assert not keys(c) & {"haber_negatif", "haber_pozitif", "haber_notr"} and "haber_az" in keys(c)
    three = neg + [{"story": f"s:{i}", "dir": -1, "imp": 3, "title": "x", "at": "2026-11-12"} for i in range(2)]
    c = karar.decide(facts(news={"events": three}))
    assert "haber_negatif" in keys(c, ("against",)) and c["stance"] == "izle"
    assert "3 ayrı haber hikâyesi" in next(s["text"] for s in c["against"] if s["key"] == "haber_negatif")


def test_unkeyed_events_grouped_by_headline_and_called_groups():
    ceo = [{"story": None, "tk": f"k{i}", "dir": -1, "imp": 4, "at": "2026-11-14",
            "title": f"Nvidia CEO Jensen Huang departs amid scandal report {s}"} for i, s in enumerate(["reuters", "bloomberg", "cnbc"] * 50)]
    t = karar.news_tone(ceo)
    assert t["events"] == 150 and t["n"] == 1 and not t["by_story"]              # hundreds of outlets, one vote
    far = [{"story": None, "tk": "z", "dir": -1, "imp": 4, "at": "2026-11-01", "title": ceo[0]["title"]}]
    assert karar.news_tone(ceo + far)["n"] == 2                                  # same headline 13 days apart → separate group
    other = [{"story": None, "tk": f"o{i}", "dir": 1, "imp": 3, "at": "2026-11-13", "title": t_} for i, t_ in
             enumerate(["Microsoft signs chip supply deal", "Quarterly revenue beats estimates widely", "New data center partnership announced"])]
    c = karar.decide(facts(news={"events": ceo + other}))
    txt = next(s["text"] for k in ("for", "against", "notes") for s in c[k] if s["key"].startswith("haber_"))
    assert "153 haber kaydı" in txt and "4 grup" in txt and "ayrı haber hikâyesi" not in txt


def test_option_dimension_is_ignored():
    f = facts()
    f["card"]["dims"]["opsiyon"] = {"score": 0.0, "parts": [{"label": "Gamma negatif", "value": "", "pts": None}]}
    assert karar.decide(f)["stance"] == "saglam"


# ── changers follow the thresholds ──

def test_changers_consistent_with_thresholds():
    c = karar.decide(facts())
    down = " ".join(x["text"] for x in c["changers"]["down"])
    assert "100,00$" in down and "90,00$" in down and f"puanı {karar.BUS_WEAK} altına" in down
    # crossing the stated levels does what the changer says
    assert karar.decide(broken(facts(), 89.0))["stance"] == "izle"
    assert karar.decide(temel(facts(), karar.BUS_WEAK - 0.1))["stance"] == "izle"
    assert karar.decide(temel(facts(), karar.BUS_WEAK))["stance"] == "saglam"
    # izle with a broken chart: reclaiming the nearer average is the way up, -25% from cost the way down
    c = karar.decide(broken(facts(), 89.0))
    assert "200 günlük ortalamanın (90,00$)" in c["changers"]["up"][0]["text"]
    assert any("75,00$" in x["text"] for x in c["changers"]["down"])
    f = broken(facts(), 74.0)
    f["position"]["unreal_pct"] = karar.LOSS_DEEP - 1
    assert karar.decide(f)["stance"] == "sorgula"
    f["position"]["unreal_pct"] = karar.LOSS_DEEP + 1
    assert karar.decide(f)["stance"] == "izle"
    f = broken(facts(), 91.0)                                     # above the 200-day → not broken
    assert karar.decide(f)["stance"] == "saglam"
    # with an exit rule the way down is the rule itself
    c = karar.decide(facts(thesis=thesis(rule())))
    assert any("Kapanış 70$ altına inerse" in x["text"] for x in c["changers"]["down"])


def test_next_and_earnings_note():
    c = karar.decide(facts(earnings=[{"day": "2026-11-18", "name": "XYZ bilanço (kapanış sonrası)"}, {"day": "2027-01-30", "name": "far"}]))
    assert c["next"] == [{"day": "2026-11-18", "text": "XYZ bilanço (kapanış sonrası)"}]
    assert "bilanco_yakin" in keys(c, ("notes",)) and c["stance"] == "saglam"


# ── board ──

def _cards():
    a = karar.decide(facts(symbol="AAA", thesis=thesis(rule(status="triggered"))))
    b = broken(temel(facts(symbol="BBB"), 30.0))
    b["position"]["weight"] = 30.0
    return [karar.decide(facts(symbol="CCC")), karar.decide(b), a]


def test_board_shape_and_order():
    pf = {"as_of": "2026-11-13", "regime": {"day": "2026-11-13", "label": "Temkinli Boğa", "score": 64.0, "tone": "pos",
                                            "weakest": {"name": "Dolar", "score": 37.0, "trigger": "Dolar yükselirse puan düşer"}},
          "regime_prev": {"day": "2026-11-06", "score": 44.0},
          "risk": {"beta": 1.8, "stock_pct": 95.0, "ceiling": 80, "warnings": [{"key": "ceiling", "text": "Hisse oranı yüksek."}]}, "recent_loss": None}
    b = karar.build_board(_cards(), pf)
    assert [c["symbol"] for c in b["cards"]] == ["AAA", "BBB", "CCC"]
    assert [x["stance"] for x in b["stance_counts"]] == ["alarm", "sorgula", "saglam"]
    assert b["agenda"][0]["symbol"] == "AAA" and b["agenda"][0]["w"] == 3 and b["agenda"][0]["href"] == "/tez#t7"
    assert b["agenda"][1]["symbol"] == "BBB" and len(b["agenda"]) <= 5
    assert any(a["symbol"] is None and a["href"] == "/tez" for a in b["agenda"])
    assert "yükseliyor" in b["market"]["text"] and "%80" in b["market"]["text"] and b["headline"].startswith("Hisse pozisyonları") and b["market"]["text"].startswith("Piyasa Temkinli Boğa")


# ── commentary ──

def test_number_guard():
    c = karar.decide(facts())
    f = [karar.card_facts(c)]
    assert karar.guard("Temel puan 80 ve fiyat 110 dolar; 50 günlük ortalama 100.", f, 60)
    assert karar.guard("Fiyat %37 yükselebilir.", f, 60) is None
    assert karar.guard("Bu hisseyi sat.", f, 60) is None
    assert karar.guard("kelime " * 61, f, 60) is None
    assert karar.guard(None, f, 60) is None


def test_fingerprint_stable_under_reordering():
    c = karar.decide(broken(facts()))
    d = copy.deepcopy(c)
    for k in ("for", "against", "notes"):
        d[k] = list(reversed(d[k]))
    assert karar.fingerprint(c) == karar.fingerprint(d)
    e = copy.deepcopy(c)
    e["stance"] = "sorgula"
    assert karar.fingerprint(e) != karar.fingerprint(c)


def _fake(calls, reply):
    def completer(prompt):
        calls.append(prompt)
        return reply, "fake:model"
    return completer


def test_comment_reuse_makes_no_call():
    cards = _cards()
    b = karar.build_board(cards, {"as_of": "2026-11-13"})
    calls: list = []
    good = '{"portfoy": "Portföyde üç hisse var.", "hisseler": {"AAA": "Kendi kuralın bozuldu.", "BBB": "Şirket de grafik de zayıf, temel 30.", "CCC": "Tablo temiz."}}'
    now = datetime(2026, 11, 16, 6, tzinfo=timezone.utc)
    assert karar.write_comments(cards, b, {}, None, _fake(calls, good), now) is True and len(calls) == 1
    by = {c["symbol"]: c for c in cards}
    assert by["AAA"]["comment"] == "Kendi kuralın bozuldu." and by["AAA"]["comment_model"] == "fake:model" and b["comment"] == "Portföyde üç hisse var."
    prev = {c["symbol"]: copy.deepcopy(c) for c in cards}
    prev_board = {"comment": b["comment"], "comment_model": b["comment_model"], "comment_at": b["comment_at"]}
    cards2, b2 = _cards(), karar.build_board(_cards(), {"as_of": "2026-11-13"})
    assert karar.write_comments(cards2, b2, prev, prev_board, _fake(calls, good), now + timedelta(days=3)) is False and len(calls) == 1
    assert {c["symbol"]: c["comment"] for c in cards2} == {s: prev[s]["comment"] for s in prev} and b2["comment"] == b["comment"]
    # older than the TTL, or a changed fingerprint → a new call
    assert karar.write_comments(_cards(), b2, prev, prev_board, _fake(calls, good), now + timedelta(days=karar.COMMENT_TTL + 1)) is True
    changed = _cards()
    changed[0] = karar.decide(broken(facts(symbol="CCC")))
    assert karar.write_comments(changed, b2, prev, prev_board, _fake(calls, good), now) is True and len(calls) == 3


def test_rejected_comment_is_retried_next_run():
    bad = '{"portfoy": "Portföyde üç hisse var.", "hisseler": {"AAA": "Kural bozuldu.", "BBB": "Zayıf.", "CCC": "Fiyat 999 olur."}}'
    calls: list = []
    now = datetime(2026, 11, 16, 6, tzinfo=timezone.utc)
    cards = _cards()
    b = karar.build_board(cards, {})
    karar.write_comments(cards, b, {}, None, _fake(calls, bad), now)
    assert {c["symbol"]: c["comment"] for c in cards}["CCC"] is None          # invented number → rejected
    prev = {c["symbol"]: copy.deepcopy(c) for c in cards}
    prev_board = {"comment": b["comment"], "comment_model": b["comment_model"], "comment_at": b["comment_at"]}
    assert karar.write_comments(_cards(), karar.build_board(_cards(), {}), prev, prev_board, _fake(calls, bad), now + timedelta(hours=1)) is True
    assert len(calls) == 2


def test_model_failure_keeps_comments_empty():
    def boom(prompt):
        raise llm.CapReached("cap")
    cards = _cards()
    b = karar.build_board(cards, {})
    karar.write_comments(cards, b, {}, None, boom)
    assert all(c["comment"] is None for c in cards) and b["comment"] is None and "comment_at" not in b


# ── storage ──

@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    db.init()


def test_save_load_board_history(tmpdb):
    cards = _cards()
    for c in cards:
        c["fingerprint"] = karar.fingerprint(c)
    b = karar.build_board(cards, {"as_of": "2026-11-13"})
    b["comment"] = "Genel yorum."
    karar.save(b)
    got = karar.board()
    assert got["comment"] == "Genel yorum." and got["run"]["id"] and [c["symbol"] for c in got["cards"]] == ["AAA", "BBB", "CCC"]
    card = karar.latest("bbb")
    assert card["stance"] == "sorgula" and card["id"] and card["created_at"] and card["pattern"] == "ikisi_zayif" and card["for"] is not None
    h = karar.history("BBB")
    assert h[0]["stance_tr"] == "Tezi sorgula" and h[0]["tone"] == "neg"
    nxt = karar.decide(broken(facts(symbol="CCC")))
    nxt["as_of"] = "2026-11-14"
    assert karar._changed(nxt) == {"from": "saglam", "from_tr": "Sağlam", "day": "2026-11-14"}
    assert karar.brief(card)["stance"] == "sorgula"
    tr = karar.track_record(20)
    assert tr["n"] == 0 and tr["cards"] == 3 and [r["stance"] for r in tr["rows"]][0] == "alarm"
    assert next(r for r in tr["rows"] if r["stance"] == "izle")["hit"] is None


def test_llm_route_override(tmpdb, monkeypatch):
    seen = []

    def fake_run(prompt, provider, model, timeout, toolsets="clarify"):
        seen.append((provider, model))
        if model == db.get_setting("karar_llm_model"):
            raise llm.LLMError("down")
        return "ok"
    monkeypatch.setattr(llm, "_run", fake_run)
    routes = [(db.get_setting("karar_llm_provider"), db.get_setting("karar_llm_model"))] + llm.default_routes()
    assert routes[0] == ("anthropic", "claude-opus-5-5")
    text, model = llm.complete("p", routes=routes, priority=True)
    assert text == "ok" and seen[:2] == [routes[0], routes[1]] and model == f"{routes[1][0]}:{routes[1][1]}"
    seen.clear()
    llm.complete("p")                                  # default behaviour unchanged
    assert seen == [llm.default_routes()[0]]


# ── SEC fallback for the business axis ──

def q(end, rev, ni, oi, yoy=None, shares=100.0):
    return {"period_end": end, "form": "10-Q", "revenue": rev, "rev_yoy": yoy, "net_income": ni, "op_income": oi, "shares_out": shares}


def sec_rows(growth, margin, shares_now=100.0, n=5):
    ends = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"][:n]
    rows = [q(e, 100.0, margin * 100, margin * 100, growth if i == 0 else None, 100.0) for i, e in enumerate(ends)]
    rows[0]["shares_out"] = shares_now
    return rows


def no_temel(f):
    f["card"]["dims"]["temel"]["score"] = None
    return f


def test_sec_fallback_reaches_each_business_state():
    # growth 30 → 100, margins 30% → 100 → strong
    c = karar.decide(facts(card=no_temel(facts())["card"], sec=sec_rows(30, 0.30)))
    assert c["business"] == {"state": "guclu", "score": 100.0, "src": "sec"} and c["stance"] == "saglam"
    sig = next(s for s in c["for"] if s["key"] == "temel_sec_guclu")
    assert "SEC" in sig["text"] and "2026-09-30" in sig["src"] and "temel (SEC) 100/100" in c["headline"]
    # growth 5 → 50, margins 8% → 50 → average
    c = karar.decide(facts(card=no_temel(facts())["card"], sec=sec_rows(5, 0.08)))
    assert c["business"]["state"] == "orta" and c["business"]["score"] == pytest.approx(50.0)
    # growth -10 → 20, margins -20% → 0 → weak; with a broken chart → sorgula
    c = karar.decide(broken(facts(card=no_temel(facts())["card"], sec=sec_rows(-10, -0.20))))
    assert c["business"]["state"] == "zayif" and c["stance"] == "sorgula" and "temel_sec_zayif" in keys(c, ("against",))
    assert any("Temel (SEC) eşdeğer puanı 45" in x["text"] for x in c["changers"]["up"])


def test_karne_temel_wins_over_sec():
    c = karar.decide(facts(sec=sec_rows(-10, -0.20)))
    assert c["business"] == {"state": "guclu", "score": 80.0, "src": "karne"} and not any(k.startswith("temel_sec") for k in keys(c))


def test_no_sec_or_too_little_stays_unknown():
    f = facts(card=no_temel(facts())["card"], sec=[])
    assert karar.decide(f)["business"]["state"] == "bilinmiyor"
    one = [q("2026-09-30", 100.0, None, None, None)]                                  # one quarter, no margins, no YoY → < 2 parts
    assert karar.decide(facts(card=no_temel(facts())["card"], sec=one))["business"]["state"] == "bilinmiyor"
    zero = sec_rows(30, 0.3)
    zero[0]["revenue"] = 0
    assert karar.decide(facts(card=no_temel(facts())["card"], sec=zero))["business"]["state"] == "bilinmiyor"
    two = [q("2026-09-30", 100.0, 30.0, 30.0, None)]                                  # latest quarter margins only: 2 parts
    s = karar.sec_temel(two)
    assert s["score"] == pytest.approx(100.0) and "son çeyrek" in s["parts"][0][0]
    gap = sec_rows(30, -1.0)                                                          # one-off gain: net +80%, operating -100%
    for r in gap:
        r["net_income"] = 80.0
    s = karar.sec_temel(gap)
    assert [p[0].split(" (")[0] for p in s["parts"]] == ["gelir büyümesi", "faaliyet marjı"] and s["score"] == pytest.approx(50.0)


def test_sec_dilution_flag_when_karne_has_none():
    rows = sec_rows(30, 0.30, shares_now=110.0)                                       # +10% shares YoY
    c = karar.decide(facts(card=no_temel(facts())["card"], sec=rows))
    assert "sulandirma" in keys(c, ("against",)) and c["stance"] == "izle"
    assert "sulandirma" not in keys(karar.decide(facts(sec=rows)), ("against",))     # karne present → karne decides


def test_current_vs_latest(tmpdb):
    cards = _cards()
    karar.save(karar.build_board(cards, {"as_of": "2026-11-13"}))
    karar.save(karar.build_board([c for c in _cards() if c["symbol"] != "BBB"], {"as_of": "2026-11-14"}))
    assert karar.latest("BBB")["stance"] == "sorgula" and karar.current("BBB") is None
    assert karar.current("AAA")["stance"] == "alarm"


def test_risk_compute_accepts_sector_lookup():
    import inspect
    from borsa_radar import risk
    assert "sector" in inspect.signature(risk._compute).parameters


def test_sec_revaluation_driven_income_gives_no_score():
    """Profit or loss several times revenue (asset revaluations, e.g. a bitcoin treasury) says nothing about the business."""
    rows = sec_rows(-2, 0.30)
    for r, oi in zip(rows, (20000.0, -8000.0, -14000.0, -17000.0, 3900.0)):   # operating result up to 200× revenue
        r["op_income"], r["net_income"] = oi, oi * 0.9
    s = karar.sec_temel(rows)
    assert s["score"] is None and s["odd"]
    c = karar.decide(broken(facts(card=no_temel(facts())["card"], sec=rows)))
    assert c["business"]["state"] == "bilinmiyor" and "temel_sec_anlamsiz" in keys(c, ("notes",))
    assert c["stance"] == "izle"                                              # not "both axes weak" on revaluation noise


def test_undecided_chart_far_below_high_or_lagging_is_not_saglam():
    """Strong business + price above the 200-day only, but 20 points behind the index / 36% off the high → izle, not saglam."""
    f = facts()
    f["tech"].update(price=95.0, above50=False, above200=True, trend_score=35.0, rs63_vs_spy=-20.7, from_hi52=-36.0)
    c = karar.decide(f)
    assert c["stance"] == "izle" and {"rs_zayif", "zirveden_uzak"} <= keys(c, ("against",))
    assert any("S&P farkı" in x["text"] for x in c["changers"]["up"])
    f["tech"].update(rs63_vs_spy=-5.0, from_hi52=-12.0)              # same undecided chart without those two → still sound
    assert karar.decide(f)["stance"] == "saglam"
