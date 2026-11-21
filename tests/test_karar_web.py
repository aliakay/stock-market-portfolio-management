"""Karar UI layer: macros, karar.html, bugun.html, context text() — with a fake engine module (no DB, no network, no model)."""

import sys
import types

import pytest

from borsa_radar import context, db, digest

STANCES = {"alarm": ("Kural ihlali", "neg", 3), "sorgula": ("Tezi sorgula", "neg", 2), "izle": ("İzle", "warn", 1),
           "saglam": ("Sağlam", "pos", 0), "veri_yok": ("Yetersiz veri", "neu", -1)}


def _card(sym, stance, **kw):
    label, tone, rank = STANCES[stance]
    c = {"symbol": sym, "as_of": "2026-11-13", "price": 100.0, "rules_ver": "v1", "stance": stance, "stance_tr": label, "tone": tone,
         "rank": rank, "pattern": "x", "pattern_tr": "Sağlam şirket, bozuk grafik", "headline": f"{sym} başlık cümlesi.",
         "for": [{"key": "a", "text": f"{sym} lehine hafif", "w": 1, "src": "karne"}, {"key": "b", "text": f"{sym} lehine ağır", "w": 3, "src": "hub"}],
         "against": [{"key": "c", "text": f"{sym} aleyhine sinyal", "w": 2, "src": "tez"}], "notes": [{"key": "n", "text": f"{sym} notu", "w": 1, "src": ""}],
         "changers": {"up": [{"text": f"{sym} yukarı değiştirici"}], "down": [{"text": f"{sym} aşağı değiştirici"}]},
         "next": [{"day": "2026-11-20", "text": f"{sym} bilanço"}], "todo": [{"text": f"{sym} tezini yaz", "href": "/tez?symbol=" + sym}],
         "position": {"weight": 12.5, "unreal_pct": -8.2, "value": 1000.0, "risk_share": 20.0, "beta": 1.4}, "changed": None,
         "comment": f"{sym} model yorumu.", "comment_model": "sonnet", "sources": ["Yahoo"], "id": 1, "created_at": "2026-11-14T00:30:00+00:00"}
    c.update(kw)
    return c


def _board():
    cards = [_card("AAA", "alarm", changed={"from": "izle", "from_tr": "İzle", "day": "2026-11-12"}),
             _card("BBB", "izle", comment=None, comment_model=None),
             _card("CCC", "saglam", **{"for": [], "against": [], "notes": [], "changers": {"up": [], "down": []}, "next": [], "todo": [],
                                       "position": None})]
    return {"as_of": "2026-11-13", "headline": "Portföyün yüzde 12 kadarı kural ihlalinde.",
            "market": {"label": "Temkinli Boğa", "score": 64.0, "tone": "warn", "text": "Piyasa temkinli."},
            "stance_counts": [{"stance": "alarm", "stance_tr": "Kural ihlali", "tone": "neg", "n": 1, "weight_pct": 12.0, "symbols": ["AAA"]},
                              {"stance": "izle", "stance_tr": "İzle", "tone": "warn", "n": 1, "weight_pct": 30.0, "symbols": ["BBB"]},
                              {"stance": "sorgula", "stance_tr": "Tezi sorgula", "tone": "neg", "n": 0, "weight_pct": 0, "symbols": []}],
            "agenda": [{"text": "AAA tezini gözden geçir", "symbol": "AAA", "href": "/tez#t1", "w": 3},
                       {"text": "Bilanço haftası", "symbol": None, "href": "/calendar", "w": 1}],
            "changes": [{"symbol": "AAA", "from_tr": "İzle", "to_tr": "Kural ihlali", "day": "2026-11-13"}],
            "cards": cards, "comment": "Genel model yorumu.", "comment_model": "sonnet", "run": {"id": 3, "created_at": "2026-11-14T00:30:00+00:00"}}


def _fake_karar(monkeypatch, board, track=None):
    m = types.ModuleType("borsa_radar.karar")
    m.RULES_VER, m.STANCES = "v1", STANCES
    m.board = lambda: board
    m.latest = lambda s: next((c for c in (board or {}).get("cards", []) if c["symbol"] == s), None)
    m.track_record = lambda horizon=20: track or {"horizon": 20, "rows": [{"stance": "saglam", "label": "Sağlam", "tone": "pos", "n": 0, "hits": 0,
                                                                         "avg": None, "hit": None}], "n": 0, "cards": 3}
    m.current = m.latest
    m.brief = lambda c: {k: c[k] for k in ("symbol", "stance", "stance_tr", "headline")}
    monkeypatch.setitem(sys.modules, "borsa_radar.karar", m)
    monkeypatch.setattr("borsa_radar.karar", m, raising=False)
    return m


@pytest.fixture
def env():
    from borsa_radar.web.app import templates
    return templates.env


def _macro(env, expr, **ctx):
    return env.from_string('{% from "_macros.html" import karar_card, karar_chip %}' + expr).render(**ctx)


def test_macros_full_card(env):
    b = _board()
    out = _macro(env, "{{ karar_card(c) }}", c=b["cards"][0])
    for t in ("Kural ihlali", "Sağlam şirket, bozuk grafik", "AAA başlık cümlesi.", "AAA model yorumu.", "Lehine", "Aleyhine", "Ne değiştirir?",
              "Sıradaki tarihler", "Yapılacak", "önceki durum: İzle", 'id="karar"', "kurallar v1", "yorum: sonnet", 'title="hub"'):
        assert t in out, t
    assert out.index("AAA lehine ağır") < out.index("AAA lehine hafif")   # heavier first


def test_macros_edge_cases(env):
    b = _board()
    out = _macro(env, "{{ karar_card(c) }}", c=b["cards"][1])
    assert "model yorumu" not in out and "önceki durum" not in out
    out = _macro(env, "{{ karar_card(c) }}", c=b["cards"][2])
    assert "Sağlam" in out and "Ne değiştirir?" not in out and "Yapılacak" not in out
    assert _macro(env, "{{ karar_card(None) }}{{ karar_chip(None) }}").strip() == ""
    assert "/h/AAA#karar" in _macro(env, "{{ karar_chip(c) }}", c=b["cards"][0])
    assert "kchip" in _macro(env, "{{ karar_card(c, compact=true, anchor='k-AAA') }}", c=b["cards"][0]) and "Sıradaki tarihler" not in \
        _macro(env, "{{ karar_card(c, compact=true) }}", c=b["cards"][0])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("borsa_radar.config.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("borsa_radar.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("borsa_radar.config.REPORTS_DIR", tmp_path / "r")
    monkeypatch.setattr("borsa_radar.config.BACKUP_DIR", tmp_path / "b")
    db.init()
    from fastapi.testclient import TestClient
    from borsa_radar.web.app import app
    return TestClient(app)   # no `with`: the scheduler is not started


def test_karar_page_with_board(client, monkeypatch):
    _fake_karar(monkeypatch, _board())
    r = client.get("/karar")
    assert r.status_code == 200
    for t in ("Portföyün yüzde 12 kadarı kural ihlalinde.", "Genel model yorumu.", "Piyasa temkinli.", "AAA tezini gözden geçir", "Sicil",
              "Örnek küçük (n = 0", "Bu sonuç nasıl hesaplanıyor?", "Yeniden hesapla", 'id="k-BBB"'):
        assert t in r.text, t
    assert client.get("/api/karar").json()["headline"].startswith("Portföyün")
    assert client.get("/api/karar/AAA").json()["symbol"] == "AAA"
    assert client.get("/api/karar/ZZZ").status_code == 404


def test_karar_page_without_board(client, monkeypatch):
    _fake_karar(monkeypatch, None)
    r = client.get("/karar")
    assert r.status_code == 200 and "Henüz karar yok" in r.text
    monkeypatch.setitem(sys.modules, "borsa_radar.karar", None)   # engine missing: import raises
    assert client.get("/karar").status_code == 200
    assert client.get("/").status_code == 200


def test_home_with_board(client, monkeypatch):
    _fake_karar(monkeypatch, _board())
    r = client.get("/")
    assert r.status_code == 200
    assert "Sonuç" in r.text and "Portföyün yüzde 12 kadarı kural ihlalinde." in r.text and "AAA tezini gözden geçir" in r.text
    assert "Genel model yorumu." in r.text and "Kural ihlali · 1" in r.text and "İzle → " in r.text


def test_context_text_karar_section(monkeypatch):
    _fake_karar(monkeypatch, _board())
    k = context.karar_block()
    assert k["headline"] and len(k["stocks"]) == 3 and k["stance_counts"][0]["stance"] == "alarm"
    t = context.text({"karar": k})
    assert "KARAR (2026-11-13): Portföyün yüzde 12 kadarı kural ihlalinde." in t and "Piyasa: Piyasa temkinli." in t
    assert "- AAA: Kural ihlali" in t and "- BBB: İzle" in t and "CCC" not in t
    assert "KARAR" not in context.text({"karar": None}) and "KARAR" not in context.text({"karar": {"error": "x"}})


def test_digest_karar_lines():
    assert digest.karar_lines(None) == []
    L = digest.karar_lines(_board())
    assert L[1] == "<b>🧭 Karar</b>" and "Kural ihlali 1" in L[2] and "AAA" in L[3] and len(L) - 1 <= 4
    b = _board()
    b["changes"] = []
    assert len(digest.karar_lines(b)) == 3


def test_cli_board_card_and_sicil(monkeypatch, capsys):
    import argparse
    from borsa_radar import cli_ext_karar
    _fake_karar(monkeypatch, _board())
    ns = lambda what=None, **k: argparse.Namespace(what=what, json=k.get("json", False), no_llm=True)  # noqa: E731
    assert cli_ext_karar.cmd_karar(ns()) == 0
    out = capsys.readouterr().out
    assert "Durumlar: Kural ihlali 1" in out and "AAA" in out and "Gündem" in out
    assert cli_ext_karar.cmd_karar(ns("aaa")) == 0 and "Lehine" in capsys.readouterr().out
    assert cli_ext_karar.cmd_karar(ns("zzz")) == 1
    assert cli_ext_karar.cmd_karar(ns("sicil")) == 0 and "Örnek küçük" in capsys.readouterr().out
    assert cli_ext_karar.cmd_karar(ns(json=True)) == 0 and '"headline"' in capsys.readouterr().out
