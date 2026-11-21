"""One compact, server-computed snapshot of everything the radar knows.

Used by three consumers so they all see the same numbers:
  * `borsa-radar context [--symbol SYM] [--json]` — the Hermes agent's single entry point,
  * the morning/evening digests (`text()` goes into the model prompt),
  * `/api/context` (same JSON over HTTP, tailnet only).
Every block carries its as-of date; nothing here calls a model.
"""

from __future__ import annotations

import json
from datetime import timedelta

from . import config, db, portfolio, prices, scorecard, setups
from .util import fmt_local, iso, utcnow


def _r(v, d=2):
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return int(round(v)) if d == 0 else round(v, d)


def portfolio_block() -> dict | None:
    led = portfolio.build()
    if not led.days:
        return None
    perf = portfolio.total_perf(led)
    per = {k: {"pct": _r(v.get("pct")), "pnl": _r(v.get("pnl"), 0), "spx": _r((v.get("bench") or {}).get("SPX"))}
           for k, v in perf["periods"].items()}
    pos = []
    for p in portfolio.positions(led=led):
        c = scorecard.latest(p["symbol"])
        pos.append({"symbol": p["symbol"], "asset": p["asset"], "qty": _r(p["qty"], 4), "avg": _r(p["avg"]), "price": _r(p["price"]),
                    "value": _r(p["value"], 0), "weight_pct": _r(p["weight"], 1), "day_pct": _r(p["day_pct"]),
                    "unreal": _r(p["unreal"], 0), "unreal_pct": _r(p["unreal_pct"], 1),
                    "karne": _r(c["overall"], 0) if c else None, "karne_day": c["as_of"] if c else None})
    return {"as_of": perf["as_of"], "value": _r(perf["value"], 0), "cash": _r(perf["cash"], 0), "net_deposits": _r(perf["deposits"], 0),
            "cagr_pct": _r(perf["cagr"]), "spx_cagr_pct": _r((perf.get("bench_cagr") or {}).get("SPX")),
            "drawdown_from_ath_pct": _r((perf.get("ath") or {}).get("dd_pct")), "periods": per, "positions": pos,
            "headline": portfolio.headline(perf), "source": "radar defteri (Revolut dökümü + elle girilenler), Yahoo kapanış"}


def regime_block() -> dict | None:
    from . import regime
    r = regime.latest()
    if not r:
        return None
    comps = [{"key": k, "name": c.get("name"), "score": _r(c.get("score"), 0), "weight": c.get("weight"),
              "why": c.get("why"), "trigger": c.get("trigger")}
             for k, c in (r.get("comps") or {}).items() if isinstance(c, dict)]
    return {"day": r["day"], "label": r["label"], "score": _r(r["overall"], 0), "climate": _r(r.get("climate"), 1),
            "weather": r.get("weather"), "summary": r.get("summary"), "components": comps, "source": "radar rejim kuralları (rules v1)"}


def setups_block() -> dict:
    rep = setups.report()
    keep = ("symbol", "kind", "name_tr", "day", "status", "status_tr", "entry_lo", "entry_hi", "stop", "t1", "t2", "rr", "quality",
            "valid_until", "trig_day", "trig_px")
    card = lambda c: {k: (_r(c[k]) if isinstance(c.get(k), float) else c.get(k)) for k in keep}  # noqa: E731
    tr = setups.track_record("live")["all"]
    bt = setups.track_record("backtest")["all"]
    disc = [card(c) | {"company": c.get("company"), "sector": c.get("sector"), "list": c.get("src")} for c in rep.get("discovery") or []]
    return {"day": rep["day"], "today": [card(c) for c in rep["cards"]], "open": [card(c) for c in rep["open"]],
            "discovery": disc, "discovery_note": "listede olmayan endeks bileşenlerinden en güçlü kurulumlar; haber/tez/karne takibi yok",
            "track_record": {"live": {"decided": tr["decided"], "hit_pct": _r(tr["hit"], 1)},
                             "backtest": {"decided": bt["decided"], "hit_pct": _r(bt["hit"], 1), "note": "geriye dönük test, canlı değil"}},
            "source": "radar setup tarayıcı (kapanış bazlı)"}


def events_block(hours: float = 24, min_imp: int = 4, symbol: str | None = None, limit: int = 15) -> list[dict]:
    where = ["e.published_at >= ?", "e.status IN ('analyzed','pending')", "COALESCE(a.importance, e.importance, 0) >= ?"]
    params: list = [iso(utcnow() - timedelta(hours=hours)), min_imp]
    if symbol:
        where.append("EXISTS (SELECT 1 FROM event_symbols s WHERE s.event_id = e.id AND s.symbol = ?)")
        params.append(symbol)
    rows = db.query(f"""SELECT e.id, e.type, e.symbol, e.title, e.published_at, COALESCE(a.importance, e.importance) imp,
                               COALESCE(a.direction, e.direction) dir, a.summary_tr
                        FROM events e LEFT JOIN analyses a ON a.event_id = e.id
                        WHERE {' AND '.join(where)} ORDER BY imp DESC, e.published_at DESC LIMIT ?""", params + [limit])
    return [{"id": r["id"], "type": r["type"], "symbol": r["symbol"], "title": r["title"], "at": fmt_local(r["published_at"]),
             "importance": r["imp"], "direction": r["dir"], "summary": r["summary_tr"]} for r in rows]


def calendar_block(hours: int = 72) -> list[dict]:
    from .collectors import macro
    return [{"at": fmt_local(c["release_at"]), "name": c["name"], "importance": c["importance"]} for c in macro.upcoming(hours)]


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:  # noqa: BLE001 - one broken block must not hide the rest
        return {"error": str(exc)[:200]}


def risk_block() -> dict | None:
    from . import risk
    return risk.brief(risk.compute())


def thesis_block() -> dict:
    from . import thesis
    ov = thesis.overview()
    return {"missing_for_positions": ov["missing"],
            "attention": [thesis.brief(t) for t in ov["attention"]],
            "active": [{"symbol": t["symbol"], "status": t["status"], "next_review": t["next_review"],
                        "rules_triggered": [r["label"] for r in t["triggered"]]} for t in ov["theses"] if t["status"] != "closed"]}


def behavior_block() -> dict | None:
    from . import behavior
    b = behavior.brief(behavior.compute())
    if not b:
        return None
    return {k: b.get(k) for k in ("trips", "hit_pct", "expectancy_usd", "profit_factor", "summary", "note")} | \
           {"findings": [f.get("text") for f in (b.get("findings") or [])][:5]}


def alerts_block() -> list[dict]:
    from . import alerts
    return [{"id": a["id"], "alert": a["label"], "repeat": bool(a["repeat"])} for a in alerts.list_()]


def karar_block() -> dict | None:
    """Last stored Karar board in compact form (None = nothing computed yet)."""
    try:
        from . import karar
    except ImportError:
        return None
    b = karar.board()
    if not b:
        return None
    return {"as_of": b.get("as_of"), "headline": b.get("headline"), "comment": b.get("comment"), "market": b.get("market"),
            "stance_counts": [{k: c.get(k) for k in ("stance", "stance_tr", "n", "weight_pct", "symbols")} for c in b.get("stance_counts") or [] if c.get("n")],
            "agenda": [{k: a.get(k) for k in ("text", "symbol", "href")} for a in b.get("agenda") or []],
            "changes": b.get("changes") or [],
            "stocks": [karar.brief(c) for c in b.get("cards") or []]}


def snapshot() -> dict:
    return {"base_url": config.PUBLIC_BASE_URL, "prices_day": (prices.last_update() or {}).get("day"),
            "portfolio": portfolio_block(), "regime": regime_block(), "setups": setups_block(),
            "risk": _safe(risk_block), "theses": _safe(thesis_block), "behavior": _safe(behavior_block), "price_alerts": _safe(alerts_block),
            "karar": _safe(karar_block), "events_24h": events_block(), "calendar_72h": calendar_block()}


def _karar_brief(sym: str) -> dict | None:
    try:
        from . import karar
    except ImportError:
        return None
    c = karar.current(sym)   # latest run only
    return karar.brief(c) if c else None


def stock(symbol: str) -> dict:
    """Everything for one symbol: hub section results, scorecard, setups, position, recent events."""
    from . import hub
    sym = symbol.upper()
    h = hub.build(sym)
    secs = {k: {"title": v["title"], "result": v["result"], "tone": v["tone"], "sources": v["sources"], "missing": v["missing"]}
            for k, v in h["sections"].items()}
    c = scorecard.latest(sym)
    card = None
    if c:
        card = {"day": c["as_of"], "overall": _r(c["overall"], 0), "label": c["label"], "summary": c["summary"],
                "dims": [{"name": d["name"], "score": _r(d["score"], 0), "weight": d["weight"]} for d in c["dims"]],
                "fair_value": _r((c.get("fair") or {}).get("fair")), "fair_upside_pct": _r((c.get("fair") or {}).get("upside"), 1),
                "fair_confidence": (c.get("fair") or {}).get("confidence")}
    t = h["tech"] or {}
    pos = [p for p in portfolio.positions() if p["symbol"] == sym]
    return {"symbol": sym, "name": h["name"], "as_of": h["as_of"], "price": _r(t.get("price")), "watched": h["watched"],
            "position": {k: _r(v) if isinstance(v, float) else v for k, v in pos[0].items() if k in
                         ("qty", "avg", "value", "unreal", "unreal_pct", "weight", "first_day")} if pos else None,
            "technicals": {k: _r(t.get(k)) for k in ("chg_20d", "chg_63d", "rs63_vs_spy", "rsi14", "from_hi52", "atr_pct")} |
                          {"trend": t.get("trend_label")},
            "sections": secs, "karne": card, "karar": _safe(_karar_brief, sym),
            "setups": [x for x in setups_block()["open"] + setups_block()["today"] if x["symbol"] == sym],
            "events_30d": events_block(hours=24 * 30, min_imp=3, symbol=sym, limit=12),
            "thesis": _safe(lambda: __import__("borsa_radar.thesis", fromlist=["x"]).brief(
                __import__("borsa_radar.thesis", fromlist=["x"]).for_symbol(sym, include_closed=True))),
            "earnings": _safe(lambda: __import__("borsa_radar.earnings", fromlist=["x"]).brief(sym)),
            "price_alerts": _safe(lambda: [a["label"] for a in __import__("borsa_radar.alerts", fromlist=["x"]).list_(sym)]),
            "url": f"{config.PUBLIC_BASE_URL}/h/{sym}"}


def text(snap: dict | None = None) -> str:
    """Plain-text version for model prompts (digests)."""
    s = snap or snapshot()
    out = []
    p = s.get("portfolio")
    if p:
        d, w, y = p["periods"].get("day", {}), p["periods"].get("week", {}), p["periods"].get("ytd", {})
        out.append(f"PORTFÖY ({p['as_of']} kapanışı): değer {p['value']}$, son gün {d.get('pct')}% ({d.get('pnl')}$; SPX {d.get('spx')}%), "
                   f"hafta {w.get('pct')}%, yılbaşından {y.get('pct')}% (SPX {y.get('spx')}%).")
        for x in p["positions"]:
            out.append(f"- {x['symbol']}: ağırlık %{x['weight_pct']}, gün {x['day_pct']}%, K/Z {x['unreal_pct']}%"
                       + (f", karne {x['karne']}" if x["karne"] is not None else ""))
    r = s.get("regime")
    if r:
        out.append(f"\nPİYASA REJİMİ ({r['day']}): {r['label']} {r['score']}/100" + (f" — {r['summary']}" if r.get("summary") else ""))
    rk = s.get("risk")
    if isinstance(rk, dict) and rk.get("beta") is not None:
        out.append(f"\nRİSK ({rk['as_of']}): beta {rk['beta']}, günlük VaR95 {rk['var95_1d_usd']}$, zirveden {rk['drawdown_from_peak_pct']}%; "
                   + "; ".join(rk["warnings"][:3]))
    th = s.get("theses")
    if isinstance(th, dict) and th.get("attention"):
        out.append("\nTEZ UYARILARI: " + "; ".join(f"{t['symbol']} ({', '.join(r['rule'] for r in t['rules'] if r['status'] == 'triggered') or 'gözden geçirme zamanı'})"
                                                  for t in th["attention"]))
    k = s.get("karar")
    if isinstance(k, dict) and k.get("headline"):
        out.append(f"\nKARAR ({k.get('as_of')}): {k['headline']}")
        mk = k.get("market") or {}
        if mk.get("text"):
            out.append(f"Piyasa: {mk['text']}")
        for c in k.get("stocks") or []:
            if c.get("stance") != "saglam" and c.get("stance_tr") != "Sağlam":
                out.append(f"- {c.get('symbol')}: {c.get('stance_tr')} — {c.get('headline')}")
    st = s.get("setups") or {}
    if st.get("today") or st.get("open"):
        out.append(f"\nSETUP'LAR ({st.get('day')}):")
        for c in st.get("today", []):
            out.append(f"- YENİ {c['symbol']} {c['name_tr']}: giriş {c['entry_lo']}-{c['entry_hi']}, stop {c['stop']}, H1 {c['t1']}, R:R 1:{c['rr']}")
        for c in st.get("open", []):
            out.append(f"- AÇIK {c['symbol']} {c['name_tr']} ({c['day']}): {c['status_tr']}")
    return "\n".join(out)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
