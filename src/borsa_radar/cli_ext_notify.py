"""CLI: `borsa-radar bildirim [--hours 24] [--symbol SYM] [--all] [--json]` — what radar sent to Telegram and why
other items were held back (push policy reasons)."""

from __future__ import annotations

import json

REASON_TR = {
    "new": "gönderildi (yeni)", "new_story": "gönderildi (yeni hikâye)", "story_update": "gönderildi (hikâyede yeni bilgi)",
    "below_threshold": "eşiğin altında", "same_story": "aynı hikâye, yeni bilgi yok", "same_story_lower": "aynı hikâye, daha az önemli",
    "story_cap": "hikâye için 48 saatlik sınır doldu", "similar_title": "benzer başlık 24 saat içinde gönderildi",
    "no_push_type": "pozisyon verisi (yalnızca özet ve hisse sayfası)", "inactive": "hisse pasif",
}


def rows(hours: int = 24, symbol: str | None = None, include_dropped: bool = False) -> list[dict]:
    from . import db
    from .notifier import headline
    from .util import iso, utcnow
    from datetime import timedelta
    q = """SELECT n.id, n.kind, n.status, n.reason, n.created_at, n.sent_at, n.tg_message_id, n.event_id, n.text,
                  e.title, e.importance, e.type, e.published_at, a.story
           FROM notifications n LEFT JOIN events e ON e.id = n.event_id LEFT JOIN analyses a ON a.event_id = n.event_id
           WHERE n.created_at >= ?""" + ("" if include_dropped else " AND n.status != 'dropped'")
    params: list = [iso(utcnow() - timedelta(hours=hours))]
    if symbol:
        q += " AND EXISTS (SELECT 1 FROM event_symbols s WHERE s.event_id = n.event_id AND s.symbol = ?)"
        params.append(symbol.upper())
    out = []
    for r in db.query(q + " ORDER BY n.created_at DESC LIMIT 300", params):
        d = dict(r)
        if d["event_id"]:
            e = db.get_event(d["event_id"])
            d["headline"] = headline(e) if e else d["title"]
            d["symbols"] = db.event_symbols(d["event_id"])
        else:
            d["headline"] = (d["text"] or "").split("\n")[0][:120]
            d["symbols"] = []
        d["reason_tr"] = REASON_TR.get(d["reason"] or "", d["reason"] or "")
        d.pop("text")
        out.append(d)
    return out


def cmd_bildirim(a) -> int:
    from .util import fmt_local
    data = rows(a.hours, a.symbol, a.all)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    if not data:
        print("bu aralıkta kayıt yok")
        return 0
    for d in data:
        when = fmt_local(d["sent_at"] or d["created_at"], "%d.%m %H:%M")
        grp = f" msg#{d['tg_message_id']}" if d["tg_message_id"] else ""
        syms = ",".join(d["symbols"]) or "-"
        imp = f" önem {d['importance']}" if d["importance"] is not None else ""
        print(f"{when} {d['kind']:<11} {d['status']:<7}{grp} {syms:<10}{imp} {d['headline'][:90]}"
              + (f"  [{d['reason_tr']}]" if d["reason_tr"] else "") + (f"  hikâye={d['story']}" if d["story"] else ""))
    return 0


def register(sub) -> None:
    p = sub.add_parser("bildirim", help="Telegram'a ne gitti, ne neden gitmedi (bildirim politikası gerekçeleri)")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--symbol")
    p.add_argument("--all", action="store_true", help="atlananları (dropped) da göster")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_bildirim)
