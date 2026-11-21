"""Telegram delivery: alert selection, quiet hours, rate limiting, formatting."""

from __future__ import annotations

import html
import logging
from datetime import timedelta

import httpx

from . import config, db
from .util import fmt_local, iso, in_quiet_hours, parse_dt, utcnow, utcnow_iso

log = logging.getLogger(__name__)

DIR_ICON = {1: "🟢", 0: "⚪", -1: "🔴", None: "⚪"}
TYPE_LABEL = {"news": "Haber", "rating": "Analist", "filing": "SEC", "macro": "Makro", "data": "Veri",
              "reg": "Düzenleme", "contract": "Devlet", "flow": "Pozisyon"}
ALERT_WINDOW_HOURS = 24
MAX_MSG = 3900


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def event_url(event_id: int) -> str:
    return f"{config.PUBLIC_BASE_URL}/e/{event_id}"


# ── sending ───────────────────────────────────────────────

def send_telegram(text: str, *, reply_to: int | None = None) -> tuple[bool, str | None, int | None]:
    """Returns (ok, error, telegram message id)."""
    token, chat = config.telegram_credentials()
    if not token or not chat:
        return False, "telegram credentials missing", None
    body = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=20)
        data = r.json()
        if not data.get("ok"):
            return False, str(data.get("description"))[:300], None
        return True, None, (data.get("result") or {}).get("message_id")
    except (httpx.HTTPError, ValueError) as exc:
        return False, str(exc)[:300], None


def enqueue(text: str, *, kind: str = "alert", event_id: int | None = None) -> int:
    return db.execute(
        "INSERT INTO notifications(event_id, kind, text, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
        (event_id, kind, text, utcnow_iso()),
    )


def send_now(text: str, *, kind: str = "system", event_id: int | None = None) -> bool:
    """Bypass queue/quiet hours (digests, explicit tests)."""
    nid = enqueue(text, kind=kind, event_id=event_id)
    ok, err, mid = send_telegram(text) if db.get_setting("telegram_enabled") == "1" else (False, "disabled", None)
    db.execute(
        "UPDATE notifications SET status = ?, sent_at = ?, error = ?, tg_message_id = ? WHERE id = ?",
        ("sent" if ok else "failed", utcnow_iso() if ok else None, err, mid, nid),
    )
    return ok


# ── formatting ────────────────────────────────────────────

CATEGORY_TR = {"earnings": "Bilanço", "guidance": "Beklenti", "analyst": "Analist", "product": "Ürün", "contract": "Sözleşme",
               "partnership": "Ortaklık", "m&a": "Birleşme/satın alma", "financing": "Finansman", "insider": "İçeriden işlem",
               "legal": "Hukuk", "regulatory": "Düzenleme", "management": "Yönetim", "macro": "Makro", "opinion": "Yorum",
               "fed": "Fed", "inflation": "Enflasyon", "jobs": "İstihdam", "growth": "Büyüme", "consumer": "Tüketici",
               "housing": "Konut", "trade": "Ticaret", "fiscal": "Maliye", "rates": "Faiz", "energy": "Enerji",
               "geopolitics": "Jeopolitik", "markets": "Piyasa"}


def _analysis(eid: int):
    return db.query_one("SELECT * FROM analyses WHERE event_id = ?", (eid,))


def _price_line(syms: list[str]) -> str | None:
    if not syms:
        return None
    try:
        from . import market
        from .util import tr_n
        q = market.quotes(syms[:3])
    except Exception:  # noqa: BLE001
        return None
    parts = []
    for s in syms[:3]:
        v = q.get(s) or {}
        if v.get("price") is None:
            continue
        ch = v.get("change_pct")
        chs = "" if ch is None else f" ({'+' if ch > 0 else '−' if ch < 0 else ''}%{tr_n(abs(ch), 1)})"
        parts.append(f"{s} {tr_n(v['price'], 2)}${chs}")
    return "💹 " + " · ".join(parts) if parts else None


def headline(e, a=None) -> str:
    """One-line Turkish headline: the analysis summary when there is one, else the source title."""
    a = a if a is not None else _analysis(e["id"])
    return (a["summary_tr"] if a and a["summary_tr"] else e["title"]) or ""


def format_event(e, *, compact: bool = False, update: bool = False) -> str:
    syms = db.event_symbols(e["id"])
    a = _analysis(e["id"])
    imp = e["importance"] or 0
    head_sym = " ".join(syms) or e["symbol"] or "MAKRO"
    icon = DIR_ICON.get(e["direction"], "⚪")
    label = CATEGORY_TR.get(a["category"], None) if a and a["category"] else None
    label = label or TYPE_LABEL.get(e["type"], e["type"])
    lines = [f"{'🔄 ' if update else ''}{icon} <b>{esc(head_sym)}</b> · {esc(label)} · önem {imp}/5" + (" · gelişme" if update else "")]
    lines.append(f"<b>{esc(headline(e, a))}</b>")
    if a and a["reasoning_tr"] and not compact:
        lines.append(f"<i>{esc(a['reasoning_tr'])}</i>")
    if a and a["portfolio_impact"]:
        imp_map = db.loads(a["portfolio_impact"], {})
        moved = {s: v for s, v in imp_map.items() if str(v) not in ("0", "")}
        if moved:
            lines.append("Portföy: " + "  ".join(f"{s} {DIR_ICON.get(int(v) if str(v).lstrip('-').isdigit() else 0)}" for s, v in moved.items()))
    if not compact and e["type"] in ("news", "filing", "rating"):
        pl = _price_line(syms or ([e["symbol"]] if e["symbol"] else []))
        if pl:
            lines.append(pl)
    lines.append(f"🕒 {fmt_local(e['published_at'], '%d.%m %H:%M')} · {esc(e['source'])} · <a href=\"{event_url(e['id'])}\">detay</a>")
    return "\n".join(lines)


# ── alert selection ───────────────────────────────────────
# Push policy (Telegram):
#   • positioning data (short interest, short volume, Reddit mentions) never pushes — digest + stock page only
#   • importance ≥ threshold (stock 4, macro 4, ratings 2); news only about watch-only names needs one more point
#   • one story pushes once; a follow-up pushes only when the model marks new facts (new_info) and it is at least as
#     important as what was already sent, at most STORY_MAX pushes per STORY_HOURS
#   • items without a story key fall back to headline similarity with what was pushed in the last day

STORY_HOURS = 48
STORY_MAX = 3
NO_PUSH_TYPES = ("flow",)


def _held() -> set[str]:
    try:
        from . import portfolio
        return {p["symbol"] for p in portfolio.positions()}
    except Exception:  # noqa: BLE001
        return set()


def _threshold(e, held: set[str] | None = None) -> int:
    if e["type"] in ("macro", "data", "reg"):
        return db.get_int("threshold_macro")
    if e["type"] == "rating":
        return 2  # every real rating / target change; pure reiterations (1) are skipped
    t = db.get_ticker(e["symbol"]) if e["symbol"] else None
    if t and t["alert_threshold"]:
        return int(t["alert_threshold"])
    base = db.get_int("threshold_stock")
    if held is not None and held and not (held & set(db.event_symbols(e["id"]) or ([e["symbol"]] if e["symbol"] else []))):
        return base + 1
    return base


_WORD = __import__("re").compile(r"[a-z0-9]{3,}")


def _words(t: str) -> set[str]:
    return set(_WORD.findall((t or "").lower())) - {"the", "and", "for", "with", "from", "after", "stock", "shares", "says"}


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def _pushed(since: str):
    return db.query("""SELECT n.event_id, e.title, e.importance, a.story FROM notifications n JOIN events e ON e.id = n.event_id
                       LEFT JOIN analyses a ON a.event_id = e.id
                       WHERE n.kind = 'alert' AND n.status IN ('queued','sent') AND n.created_at >= ?""", (since,))


def decide(e, held: set[str] | None = None) -> tuple[bool, str]:
    """(push?, reason). Reason is stored with the notification row for transparency."""
    if e["type"] in NO_PUSH_TYPES:
        return False, "no_push_type"
    if (e["importance"] or 0) < _threshold(e, held):
        return False, "below_threshold"
    if e["symbol"]:
        t = db.get_ticker(e["symbol"])
        if t and not t["active"]:
            return False, "inactive"
    a = _analysis(e["id"])
    story = a["story"] if a else None
    if story:
        prev = [r for r in _pushed(iso(utcnow() - timedelta(hours=STORY_HOURS))) if r["story"] == story]
        if prev:
            if not (a["new_info"] or 0):
                return False, "same_story"
            if len(prev) >= STORY_MAX:
                return False, "story_cap"
            if (e["importance"] or 0) < max(r["importance"] or 0 for r in prev):
                return False, "same_story_lower"
            return True, "story_update"
        return True, "new_story"
    recent = _pushed(iso(utcnow() - timedelta(hours=24)))
    if any(similar(e["title"], r["title"]) or (r["story"] is None and similar(headline(e, a), r["title"])) for r in recent):
        return False, "similar_title"
    return True, "new"


def select_alerts() -> int:
    """Queue alerts for newly analyzed events that pass the push policy."""
    since = iso(utcnow() - timedelta(hours=ALERT_WINDOW_HOURS))
    rows = db.query(
        """SELECT e.* FROM events e
           WHERE e.status = 'analyzed' AND e.published_at >= ?
             AND COALESCE(json_extract(e.extra, '$.manual'), 0) = 0
             AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.event_id = e.id AND n.kind = 'alert')
           ORDER BY e.published_at""",
        (since,),
    )
    if not rows:
        return 0
    held = _held()
    n = 0
    for e in rows:
        push, reason = decide(e, held)
        if not push:
            db.execute("INSERT INTO notifications(event_id, kind, text, status, created_at, reason) VALUES (?, 'alert', '', 'dropped', ?, ?)",
                       (e["id"], utcnow_iso(), reason))
            continue
        nid = enqueue(format_event(e, update=reason == "story_update"), kind="alert", event_id=e["id"])
        db.execute("UPDATE notifications SET reason = ? WHERE id = ?", (reason, nid))
        n += 1
    return n


# ── delivery ──────────────────────────────────────────────
# Order per flush: urgent personal items (price/thesis alerts, setups, reminders, digests) one by one → all queued news
# alerts as ONE message (grouped by symbol when there are several) → research replies under their alert.

def _group_text(rows, *, overnight: bool) -> str:
    evs = []
    for q in rows:
        e = db.get_event(q["event_id"])
        if e:
            evs.append(e)
    groups: dict[str, list] = {}
    for e in evs:
        syms = db.event_symbols(e["id"])
        key = syms[0] if syms else (e["symbol"] or "MAKRO")
        groups.setdefault(key, []).append(e)
    head = f"🌙 <b>Gece boyunca · {len(evs)} gelişme</b>" if overnight else f"📰 <b>{len(evs)} yeni gelişme</b>"
    out = [head]
    order = sorted(groups.items(), key=lambda kv: -max(x["importance"] or 0 for x in kv[1]))
    for key, items in order:
        out.append(f"\n<b>{esc(key)}</b>")
        for e in sorted(items, key=lambda x: -(x["importance"] or 0)):
            h = headline(e)
            h = h if len(h) <= 170 else h[:169].rsplit(" ", 1)[0] + "…"
            out.append(f"{DIR_ICON.get(e['direction'], '⚪')} {esc(h)} <i>({e['importance'] or 0}/5)</i> "
                       f"<a href=\"{event_url(e['id'])}\">›</a>")
    text = "\n".join(out)
    if len(text) > MAX_MSG:
        text = text[:MAX_MSG].rsplit("\n", 1)[0] + f"\n… tamamı: {config.PUBLIC_BASE_URL}/"
    return text


def _alert_message_id(event_id: int) -> tuple[str | None, int | None]:
    r = db.query_one("SELECT status, tg_message_id FROM notifications WHERE kind = 'alert' AND event_id = ? "
                     "AND status != 'dropped' ORDER BY id DESC LIMIT 1", (event_id,))
    return (r["status"], r["tg_message_id"]) if r else (None, None)


def flush() -> int:
    """Send queued messages, honoring quiet hours and a rolling rate limit."""
    if db.get_setting("telegram_enabled") != "1":
        return 0
    if in_quiet_hours(start=db.get_setting("quiet_start"), end=db.get_setting("quiet_end")):
        return 0
    queued = db.query("SELECT * FROM notifications WHERE status = 'queued' ORDER BY created_at")
    if not queued:
        return 0
    # grouped sends share one telegram message id and count once
    sent_recent = db.query_one(
        "SELECT COUNT(DISTINCT COALESCE(tg_message_id, -id)) AS n FROM notifications WHERE status = 'sent' AND sent_at >= ?",
        (iso(utcnow() - timedelta(minutes=10)),),
    )["n"]
    budget = max(0, db.get_int("rate_limit_per_10min") - sent_recent)
    if budget == 0:
        return 0

    alerts = [q for q in queued if q["kind"] == "alert" and q["event_id"]]
    research = [q for q in queued if q["kind"] == "research"]
    personal = [q for q in queued if q not in alerts and q not in research]
    plan: list[tuple[list[int], str, int | None]] = [([q["id"]], q["text"], None) for q in personal]
    if len(alerts) == 1:
        plan.append(([alerts[0]["id"]], alerts[0]["text"], None))
    elif alerts:
        overnight = any(parse_dt(q["created_at"]) < utcnow() - timedelta(hours=1) for q in alerts)
        plan.append(([q["id"] for q in alerts], _group_text(alerts, overnight=overnight), None))
    alert_ids = {q["event_id"] for q in alerts}
    for q in research:
        if q["event_id"] in alert_ids:
            continue  # its alert goes out in this flush; reply on the next one
        status, mid = _alert_message_id(q["event_id"]) if q["event_id"] else (None, None)
        if status == "queued":
            continue
        plan.append(([q["id"]], q["text"], mid))

    sent = 0
    for ids, text, reply_to in plan[:budget]:
        ok, err, mid = send_telegram(text, reply_to=reply_to)
        now = utcnow_iso()
        for i in ids:
            db.execute(
                "UPDATE notifications SET status = ?, sent_at = ?, error = ?, tg_message_id = ? WHERE id = ?",
                ("sent" if ok else "queued", now if ok else None, err, mid, i),
            )
        if not ok:
            log.warning("telegram send failed: %s", err)
            break
        sent += 1
    return sent
