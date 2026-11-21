"""Thesis notebook (tez defteri): why I own a stock, what would prove me wrong, when to look again.

Inspired by the "thesis lifecycle + invalidation criteria + scheduled review + postmortem" pattern of
open-source trader-memory tools, but stored in radar's own DB so the web UI, Telegram and Hermes share it.
Rules are checked every night against stored closes, karne, SEC quarters and the regime; nothing here
calls a model.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from . import config, db, portfolio, prices, scorecard
from .util import tr_n, utcnow_iso

log = logging.getLogger(__name__)

STATUS_TR = {"idea": "Fikir", "active": "Aktif", "review": "Gözden geçir", "closed": "Kapandı"}
KINDS = ["uzun vade", "büyüme", "momentum", "dönüş", "bilanço", "temettü", "spekülatif"]
RULES = {  # kind: (label template, unit, good_when_triggered)
    "price_below": ("Kapanış {p}$ altına inerse", "$", False),
    "price_above": ("Kapanış {p}$ üstüne çıkarsa (hedef)", "$", True),
    "loss_pct": ("Maliyetin %{p} altına inerse", "%", False),
    "karne_below": ("Karne {p} altına düşerse", "", False),
    "rev_growth_below": ("Yıllık gelir büyümesi %{p} altına inerse (SEC çeyrek)", "%", False),
    "dilution_above": ("Hisse sayısı bir çeyrekte %{p}'ten fazla artarsa", "%", False),
    "regime_below": ("Piyasa rejimi {p} altına inerse", "", False),
    "deadline": ("{p} tarihine kadar gerçekleşmezse: {note}", "", False),
    "manual": ("{p}", "", False),
}


def _today() -> str:
    return datetime.now(config.NY_TZ).date().isoformat()


def rule_label(r: dict) -> str:
    tpl = RULES.get(r["kind"], ("{p}", "", False))[0]
    p = r.get("param") or ""
    try:
        f = float(p)
        p = tr_n(f, 2).removesuffix(",00")
    except (TypeError, ValueError):
        pass
    return tpl.format(p=p, note=r.get("note") or "")


def parse_rule(text: str) -> tuple[str, str, str | None]:
    """'price_below 150', 'stop 150', 'hedef 300', 'zarar 30', 'karne 40', 'gelir 20', 'sulandırma 5',
    'rejim 40', 'tarih 2027-03-31 yeni ürün çıkmazsa', anything else → manual."""
    t = text.strip()
    head, _, rest = t.partition(" ")
    alias = {"stop": "price_below", "altı": "price_below", "price_below": "price_below", "hedef": "price_above",
             "üstü": "price_above", "price_above": "price_above", "zarar": "loss_pct", "loss_pct": "loss_pct",
             "karne": "karne_below", "karne_below": "karne_below", "gelir": "rev_growth_below", "rev_growth_below": "rev_growth_below",
             "sulandırma": "dilution_above", "dilution_above": "dilution_above", "rejim": "regime_below",
             "regime_below": "regime_below", "tarih": "deadline", "deadline": "deadline"}
    kind = alias.get(head.lower())
    if not kind:
        return "manual", t, None
    if kind == "deadline":
        d, _, note = rest.partition(" ")
        date.fromisoformat(d)
        return kind, d, note or None
    val = rest.split()[0].replace("%", "").replace("$", "").replace(",", ".") if rest else ""
    float(val)
    return kind, val, (rest.partition(" ")[2] or None)


# ── CRUD ──

def add(symbol: str, text: str, *, kind: str | None = None, catalysts: str | None = None, target: float | None = None,
        horizon_months: int | None = None, conviction: int | None = None, review_days: int = 30,
        rules: list[str] | None = None, status: str = "active") -> int:
    sym = symbol.upper().strip()
    pos = next((p for p in portfolio.positions() if p["symbol"] == sym), None)
    now = utcnow_iso()
    tid = db.execute("""INSERT INTO theses(symbol, status, kind, thesis, catalysts, target, horizon_months, conviction, review_days,
                                           next_review, entry_price, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (sym, status, kind, text.strip(), catalysts, target, horizon_months, conviction, review_days,
                      (date.today() + timedelta(days=review_days)).isoformat(), pos["avg"] if pos else None, now, now))
    note(tid, "Tez yazıldı" + (f" (maliyet {tr_n(pos['avg'], 2)}$)" if pos else ""), kind="status")
    if target:
        add_rule(tid, "price_above", str(target))
    for r in rules or []:
        k, p, n = parse_rule(r)
        add_rule(tid, k, p, n)
    check(tid)
    return tid


def add_rule(tid: int, kind: str, param: str, note_: str | None = None) -> int:
    if kind not in RULES:
        raise ValueError(f"bilinmeyen kural: {kind}")
    rid = db.execute("INSERT INTO thesis_rules(thesis_id, kind, param, note) VALUES (?,?,?,?)", (tid, kind, param, note_))
    note(tid, "Kural eklendi: " + rule_label({"kind": kind, "param": param, "note": note_}), kind="rule")
    return rid


def delete_rule(rid: int) -> None:
    db.execute("DELETE FROM thesis_rules WHERE id = ?", (rid,))


def note(tid: int, text: str, *, kind: str = "note", event_id: int | None = None) -> None:
    db.execute("INSERT INTO thesis_notes(thesis_id, ts, kind, text, event_id) VALUES (?,?,?,?,?)",
               (tid, utcnow_iso(), kind, text.strip(), event_id))
    db.execute("UPDATE theses SET updated_at = ? WHERE id = ?", (utcnow_iso(), tid))


def update(tid: int, **fields) -> None:
    allowed = {"thesis", "kind", "catalysts", "target", "horizon_months", "conviction", "review_days", "status"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    db.execute(f"UPDATE theses SET {', '.join(k + ' = ?' for k in sets)}, updated_at = ? WHERE id = ?",
               (*sets.values(), utcnow_iso(), tid))
    if "thesis" in sets:
        note(tid, "Tez metni güncellendi", kind="status")


def reviewed(tid: int, text: str | None = None, keep: bool = True) -> None:
    """Mark a review done: push next_review forward and record what was decided."""
    t = get(tid)
    nxt = (date.today() + timedelta(days=t["review_days"] or 30)).isoformat()
    db.execute("UPDATE theses SET next_review = ?, status = CASE WHEN status = 'review' THEN 'active' ELSE status END, updated_at = ? WHERE id = ?",
               (nxt, utcnow_iso(), tid))
    note(tid, ("Gözden geçirildi: tez sağlam" if keep else "Gözden geçirildi: tez zayıfladı") + (f" — {text}" if text else "") +
         f" · sonraki {nxt}", kind="review")


def close(tid: int, lessons: str | None = None) -> dict:
    t = get(tid)
    trips = [c for c in portfolio.closed_positions() if c["symbol"] == t["symbol"] and c["close"] >= t["created_at"][:10]]
    pos = next((p for p in portfolio.positions() if p["symbol"] == t["symbol"]), None)
    if trips:
        c = trips[0]
        outcome = {"realized": round(c["realized"], 2), "pct": round(c["pct"], 2) if c["pct"] is not None else None,
                   "days": c["days"], "closed": c["close"], "source": "kapanan tur"}
    elif pos:
        outcome = {"realized": None, "unreal": round(pos["unreal"], 2), "pct": round(pos["unreal_pct"] or 0, 2), "source": "pozisyon hâlâ açık"}
    else:
        outcome = {"source": "pozisyon bulunamadı"}
    db.execute("UPDATE theses SET status = 'closed', closed_at = ?, outcome = ?, lessons = ?, updated_at = ? WHERE id = ?",
               (utcnow_iso(), json.dumps(outcome), lessons, utcnow_iso(), tid))
    note(tid, "Tez kapatıldı" + (f" — ders: {lessons}" if lessons else ""), kind="status")
    return outcome


def delete(tid: int) -> None:
    for tbl in ("thesis_rules", "thesis_notes"):
        db.execute(f"DELETE FROM {tbl} WHERE thesis_id = ?", (tid,))
    db.execute("DELETE FROM theses WHERE id = ?", (tid,))


# ── reads ──

def get(tid: int) -> dict | None:
    r = db.query_one("SELECT * FROM theses WHERE id = ?", (tid,))
    return _enrich(dict(r)) if r else None


def for_symbol(symbol: str, include_closed: bool = False) -> dict | None:
    r = db.query_one("SELECT * FROM theses WHERE symbol = ? " + ("" if include_closed else "AND status != 'closed' ") +
                     "ORDER BY (status = 'closed'), id DESC LIMIT 1", (symbol.upper(),))
    return _enrich(dict(r)) if r else None


def all_(include_closed: bool = True) -> list[dict]:
    rows = db.query("SELECT * FROM theses " + ("" if include_closed else "WHERE status != 'closed' ") + "ORDER BY status = 'closed', updated_at DESC")
    return [_enrich(dict(r)) for r in rows]


def _enrich(t: dict) -> dict:
    t["rules"] = [dict(r) | {"label": rule_label(dict(r)), "good": RULES.get(r["kind"], ("", "", False))[2]}
                  for r in db.query("SELECT * FROM thesis_rules WHERE thesis_id = ? ORDER BY id", (t["id"],))]
    t["notes"] = [dict(n) for n in db.query("SELECT * FROM thesis_notes WHERE thesis_id = ? ORDER BY ts DESC LIMIT 40", (t["id"],))]
    t["status_tr"] = STATUS_TR.get(t["status"], t["status"])
    t["review_due"] = bool(t["next_review"] and t["status"] != "closed" and t["next_review"] <= date.today().isoformat())
    t["triggered"] = [r for r in t["rules"] if r["status"] == "triggered"]
    t["outcome"] = json.loads(t["outcome"]) if t.get("outcome") else None
    return t


def overview() -> dict:
    """Open positions with/without a thesis, theses needing attention."""
    pos = portfolio.positions()
    theses = all_()
    open_t = {t["symbol"]: t for t in theses if t["status"] != "closed"}
    held = {p["symbol"] for p in pos}
    return {"positions": [p | {"thesis": open_t.get(p["symbol"])} for p in pos],
            "missing": [p["symbol"] for p in pos if p["symbol"] not in open_t],
            "attention": [t for t in open_t.values() if t["triggered"] or t["review_due"] or t["status"] == "review"],
            "orphans": [t for t in open_t.values() if t["status"] == "active" and t["symbol"] not in held],
            "ideas": [t for t in theses if t["status"] == "idea"],
            "closed": [t for t in theses if t["status"] == "closed"],
            "theses": theses}


# ── nightly check ──

def _eval(rule: dict, t: dict, ctx: dict) -> tuple[str, str | None]:
    k, p = rule["kind"], rule["param"]
    sym = t["symbol"]
    if k in ("price_below", "price_above"):
        last = ctx["close"](sym)
        if not last:
            return "unknown", None
        d, px = last
        hit = px < float(p) if k == "price_below" else px > float(p)
        return ("triggered" if hit else "ok"), f"{tr_n(px, 2)}$ ({d})"
    if k == "loss_pct":
        pos = ctx["pos"].get(sym)
        if not pos or pos["unreal_pct"] is None:
            return "unknown", "pozisyon yok"
        return ("triggered" if pos["unreal_pct"] < -float(p) else "ok"), f"K/Z %{tr_n(pos['unreal_pct'], 1)}"
    if k == "karne_below":
        c = scorecard.latest(sym)
        if not c:
            return "unknown", None
        return ("triggered" if c["overall"] < float(p) else "ok"), f"karne {tr_n(c['overall'], 0)} ({c['as_of']})"
    if k in ("rev_growth_below", "dilution_above"):
        from .collectors import xbrl
        q = xbrl.quarter_table(sym, 2)
        if not q:
            return "unknown", "SEC çeyrek verisi yok"
        r = q[0]
        if k == "rev_growth_below":
            if r["rev_yoy"] is None:
                return "unknown", None
            return ("triggered" if r["rev_yoy"] < float(p) else "ok"), f"gelir YoY %{tr_n(r['rev_yoy'], 1)} ({r['period_end']})"
        if r["shares_qoq"] is None:
            return "unknown", None
        return ("triggered" if r["shares_qoq"] > float(p) else "ok"), f"hisse sayısı QoQ %{tr_n(r['shares_qoq'], 1, True)} ({r['period_end']})"
    if k == "regime_below":
        reg = ctx["regime"]
        if not reg:
            return "unknown", None
        return ("triggered" if reg["overall"] < float(p) else "ok"), f"rejim {tr_n(reg['overall'], 0)} ({reg['day']})"
    if k == "deadline":
        return ("triggered" if date.today().isoformat() > p else "ok"), f"bugün {date.today().isoformat()}"
    return "unknown", "elle kontrol"


def _ctx() -> dict:
    from . import regime
    cache: dict = {}

    def close(sym):
        if sym not in cache:
            cache[sym] = prices.series(sym, (date.today() - timedelta(days=15)).isoformat()).last()
        return cache[sym]
    return {"close": close, "pos": {p["symbol"]: p for p in portfolio.positions()}, "regime": regime.latest()}


def check(tid: int | None = None, *, alert: bool = False) -> list[dict]:
    """Evaluate rules; record changes; optionally send one Telegram note per newly triggered rule."""
    ts = [get(tid)] if tid else all_(include_closed=False)
    ctx = _ctx()
    fired = []
    for t in ts:
        if not t or t["status"] in ("closed", "idea"):
            continue
        for r in t["rules"]:
            st, val = _eval(r, t, ctx)
            if st == "triggered" and r["status"] != "triggered":
                db.execute("UPDATE thesis_rules SET status=?, last_value=?, checked_at=?, triggered_at=? WHERE id=?",
                           (st, val, utcnow_iso(), utcnow_iso(), r["id"]))
                note(t["id"], f"Kural tetiklendi: {r['label']} — {val}", kind="rule")
                fired.append({"thesis": t, "rule": r, "value": val})
            else:
                db.execute("UPDATE thesis_rules SET status=?, last_value=?, checked_at=? WHERE id=?",
                           (st, val, utcnow_iso(), r["id"]))
        if any(f["thesis"]["id"] == t["id"] and not f["rule"]["good"] for f in fired):
            db.execute("UPDATE theses SET status = 'review', updated_at = ? WHERE id = ?", (utcnow_iso(), t["id"]))
        _attach_events(t)
    if alert and fired and db.get_setting("thesis_alerts") != "0":
        from . import notifier
        for f in fired:
            icon = "🎯" if f["rule"]["good"] else "🧭"
            notifier.enqueue(f"{icon} <b>Tez uyarısı · {f['thesis']['symbol']}</b>\nKural: {f['rule']['label']}\nŞu an: {f['value']}\n"
                             f"Tez: {f['thesis']['thesis'][:220]}\n🔗 {config.PUBLIC_BASE_URL}/tez#t{f['thesis']['id']}", kind="thesis")
    return fired


def _attach_events(t: dict) -> None:
    """Link important events (importance ≥4) since the last attached one to the thesis timeline."""
    last = db.query_one("SELECT MAX(ts) m FROM thesis_notes WHERE thesis_id = ? AND kind = 'event'", (t["id"],))
    since = (last["m"] if last and last["m"] else t["created_at"])
    rows = db.query("""SELECT e.id, e.title, e.published_at, COALESCE(a.importance, e.importance) imp, COALESCE(a.direction, e.direction) d
                       FROM events e JOIN event_symbols s ON s.event_id = e.id LEFT JOIN analyses a ON a.event_id = e.id
                       WHERE s.symbol = ? AND e.published_at > ? AND e.status = 'analyzed' AND COALESCE(a.importance, e.importance, 0) >= 4
                       ORDER BY e.published_at LIMIT 10""", (t["symbol"], since))
    for e in rows:
        mark = "🟢" if (e["d"] or 0) > 0 else "🔴" if (e["d"] or 0) < 0 else "⚪"
        db.execute("INSERT INTO thesis_notes(thesis_id, ts, kind, text, event_id) VALUES (?,?,?,?,?)",
                   (t["id"], e["published_at"], "event", f"{mark} önem {e['imp']}: {e['title']}", e["id"]))


def daily_job() -> None:
    try:
        n = check(alert=True)
        log.info("thesis check: %d triggered", len(n))
    except Exception:  # noqa: BLE001
        log.exception("thesis check failed")


def brief(t: dict | None) -> dict | None:
    if not t:
        return None
    return {"id": t["id"], "symbol": t["symbol"], "status": t["status"], "kind": t["kind"], "thesis": t["thesis"],
            "catalysts": t["catalysts"], "target": t["target"], "conviction": t["conviction"], "next_review": t["next_review"],
            "review_due": t["review_due"], "created": t["created_at"][:10], "entry_price": t["entry_price"],
            "rules": [{"id": r["id"], "rule": r["label"], "status": r["status"], "value": r["last_value"]} for r in t["rules"]],
            "recent_notes": [{"at": n["ts"][:10], "kind": n["kind"], "text": n["text"]} for n in t["notes"][:8]],
            "outcome": t["outcome"], "lessons": t["lessons"]}
