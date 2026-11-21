"""Morning / evening Telegram digests and calendar reminders."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from . import config, db, llm, market, notifier
from .collectors import kalshi, macro
from .notifier import esc
from .util import fmt_local, iso, parse_dt, tr_n, utcnow

log = logging.getLogger(__name__)

COMMENT_PROMPT = """Sen kişisel bir piyasa asistanısın; kullanıcı Türk, ABD hisseleri tutuyor.
Aşağıdaki VERİLER sunucu tarafından hesaplandı ve kullanıcıya yorumunun hemen altında tablo olarak gösterilecek.
Senin işin yalnızca kısa bir YORUM yazmak:
- İlk satır: 2-4 cümle. {focus}
- Sonra her biri ayrı satırda, "Dikkat:" ile başlayan 1-2 kısa madde ({when} neye bakmalı).
Kurallar:
- Türkçe, düz metin; Markdown, **, #, tablo YOK. En fazla 600 karakter.
- RAKAM YAZMA (yüzde, fiyat, puan, saat dahil): rakamlar zaten tabloda. "sert yükseldi", "yarın akşam" gibi kelimelerle anlat.
- Bilmediğin ya da doğrulayamadığın bir şeyden hiç bahsetme; "doğrulayamadım", "kaynak bulamadım" gibi cümleler YAZMA.
- Alım/satım tavsiyesi verme.
{web_rule}

VERİLER:
{data}"""

TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
MARKET = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "Russell 2000"), ("^VIX", "VIX"), ("^TNX", "ABD 10Y"),
          ("DX-Y.NYB", "Dolar end."), ("CL=F", "Petrol"), ("GC=F", "Altın"), ("BTC-USD", "Bitcoin")]
_BAD_REPLY = ("No reply:", "Send `continue`", "didn't produce a reply", "Traceback")


def _pct(v, d: int = 1) -> str:
    if v is None:
        return "—"
    if round(abs(v), d) == 0:
        return f"%{tr_n(0, d)}"
    return ("+" if v > 0 else "−") + f"%{tr_n(abs(v), d)}"


def _d(day) -> str:
    """'2026-11-13' → '13.11'."""
    return f"{day[8:10]}.{day[5:7]}" if day and len(str(day)) >= 10 else (day or "")


_FED_TR = {"Hike": "Artırım", "Cut": "İndirim", "Hold": "Sabit", "No change": "Sabit", "Maintain": "Sabit"}


def _fed_label(t: str) -> str:
    for en, tr in _FED_TR.items():
        t = t.replace(en, tr)
    return t.replace("bps", " bp")


def _arrow(v) -> str:
    return "▲" if (v or 0) > 0 else "▼" if (v or 0) < 0 else "•"


def _short(t: str, n: int = 150) -> str:
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


KARAR_CURSOR = "karar_digest_run"   # id of the last Karar run whose stance changes went out in a digest


def _mark_karar_sent() -> None:
    try:
        from . import karar
        b = karar.board()
        if b and (b.get("run") or {}).get("id") is not None:
            db.set_cursor(KARAR_CURSOR, str(b["run"]["id"]))
    except Exception:  # noqa: BLE001
        log.exception("digest: karar cursor failed")


def karar_lines(board: dict | None) -> list[str]:
    """Quiet Karar block for the morning digest: stance counts + changes since the previous run (max 3 lines after the title)."""
    if not board:
        return []
    counts = [f"{esc(c['stance_tr'])} {c['n']}" for c in board.get("stance_counts") or [] if c.get("n")]
    if not counts:
        return []
    L = ["", "<b>🧭 Karar</b>", " · ".join(counts)]
    ch = board.get("changes") or []
    if ch:
        L.append("Değişen: " + "; ".join(f"<b>{esc(c['symbol'])}</b> {esc(c['from_tr'])} → {esc(c['to_tr'])}" for c in ch[:4])
                 + (f" (+{len(ch) - 4})" if len(ch) > 4 else ""))
    return L


def gather(kind: str) -> dict:
    """Everything the digest shows, computed by the server."""
    from . import portfolio, regime, setups
    hours = 18 if kind == "morning" else 10
    since = iso(utcnow() - timedelta(hours=hours))
    d: dict = {"kind": kind, "hours": hours, "now": datetime.now(config.LOCAL_TZ)}
    held = {p["symbol"]: p for p in portfolio.positions()}
    watch = [t["symbol"] for t in db.active_tickers() if t["symbol"] not in held]
    q = market.quotes([s for s, _ in MARKET] + list(held) + watch)
    d["market"] = [(name, sym, q.get(sym) or {}) for sym, name in MARKET]
    # portfolio: live quotes → today's estimated P/L (evening); last close ledger (morning)
    led = portfolio.build()
    if kind == "evening":
        pnl = sum(p["qty"] * p["multiplier"] * ((q.get(s) or {}).get("price", 0) - (q.get(s) or {}).get("prev_close", 0))
                  for s, p in held.items() if (q.get(s) or {}).get("price") and (q.get(s) or {}).get("prev_close"))
        val = sum(p["qty"] * p["multiplier"] * ((q.get(s) or {}).get("price") or p["price"] or 0) for s, p in held.items()) + \
              (led.total[-1]["cash"] if led.total else 0)
        d["pf"] = {"value": val, "pnl": pnl, "pct": pnl / (val - pnl) * 100 if val - pnl > 0 else None, "basis": "anlık fiyatlarla tahmini"}
    elif led.days:
        perf = portfolio.total_perf(led)
        dd = perf["periods"].get("day", {})
        d["pf"] = {"value": perf["value"], "pnl": dd.get("pnl"), "pct": dd.get("pct"), "basis": f"{_d(perf['as_of'])} kapanışı",
                   "ytd": perf["periods"].get("ytd", {}).get("pct"), "ytd_spx": (perf["periods"].get("ytd", {}).get("bench") or {}).get("SPX")}
    d["karar"] = None
    if kind == "morning":
        try:
            from . import karar
            b = karar.board()
            # stance changes are reported once: a run that an earlier digest already carried keeps only its counts
            if b and b.get("changes") and str((b.get("run") or {}).get("id")) == (db.get_cursor(KARAR_CURSOR) or ""):
                b = {**b, "changes": []}
            d["karar"] = b
        except Exception:  # noqa: BLE001 - no engine / no run yet: the digest simply has no Karar block
            log.exception("digest: karar block failed")
    # per-stock: price move + most important development in the window
    ev = db.query("""SELECT e.id, e.type, e.title, e.published_at, COALESCE(a.importance, e.importance, 0) imp,
                            COALESCE(a.direction, e.direction) dir, a.summary_tr, s.symbol
                     FROM events e JOIN event_symbols s ON s.event_id = e.id LEFT JOIN analyses a ON a.event_id = e.id
                     WHERE e.published_at >= ? AND e.status IN ('analyzed','pending') AND COALESCE(a.importance, e.importance, 0) >= 3
                     ORDER BY imp DESC, e.published_at DESC""", (since,))
    top: dict[str, dict] = {}
    for e in ev:
        top.setdefault(e["symbol"], dict(e))
    rows = []
    for s in list(held) + watch:
        v = q.get(s) or {}
        rows.append({"symbol": s, "held": s in held, "price": v.get("price"), "chg": v.get("change_pct"),
                     "weight": held[s]["weight"] if s in held else None, "event": top.get(s)})
    d["stocks"] = sorted(rows, key=lambda r: (not r["held"], -abs(r["chg"] or 0)))
    d["macro"] = [dict(r) for r in db.query(
        """SELECT e.id, e.type, e.title, COALESCE(a.importance, e.importance, 0) imp, a.summary_tr
           FROM events e LEFT JOIN analyses a ON a.event_id = e.id
           WHERE e.published_at >= ? AND e.type IN ('macro','data','reg') AND e.status IN ('analyzed','pending')
             AND COALESCE(a.importance, e.importance, 0) >= 3 ORDER BY imp DESC, e.published_at DESC LIMIT 5""", (since,))]
    d["regime"] = regime.latest()
    rep = setups.report()
    d["setups"] = {"day": rep["day"], "new": rep["cards"], "open": [o for o in setups.open_setups() if o["status"] in ("triggered", "t1")]}
    try:
        from . import alerts, thesis
        ov = thesis.overview()
        d["theses"] = ov["attention"]
        d["alerts"] = [a for a in alerts.list_(include_inactive=True) if a["fired_at"] and a["fired_at"] >= since]
    except Exception:  # noqa: BLE001
        log.exception("digest: thesis/alerts section failed")
        d["theses"], d["alerts"] = [], []
    d["calendar"] = [c for c in macro.upcoming(36) if (c["importance"] or 0) >= 3]
    d["strateji"] = None
    if kind == "morning":
        try:
            from . import strateji
            if strateji.STRATEGIES:
                from . import lab_stress
                # a strategy the stress-test judge rejected keeps its paper record on the page but leaves the digest
                bad = {st.code for st in strateji.STRATEGIES if (v := lab_stress.verdict(st.code)) and not v["passed"]}
                sig = [t for t in strateji.signals() if t["in_book"] and t["status"] == "pending" and t["strategy"] not in bad]
                d["strateji"] = {"day": sig[0]["sig_day"] if sig else None, "new": sig,
                                 "exits": [t for t in strateji.open_trades() if t.get("exit_next") and t["strategy"] not in bad]}
        except Exception:  # noqa: BLE001 - no lab yet: no block
            log.exception("digest: strateji block failed")
    try:
        groups = kalshi.overview()
        fed = next((g for g in groups if g["kind"] == "categorical" and g["items"]), None)
        d["fed"] = {"title": fed["label"], "close": fed["close"], "items": fed["items"][:2]} if fed else None
    except Exception:  # noqa: BLE001
        d["fed"] = None
    return d


def render(d: dict, comment: str | None, comment_note: str | None = None) -> str:
    kind = d["kind"]
    now = d["now"]
    title = "☀️ Sabah özeti" if kind == "morning" else "🌆 Akşam özeti"
    L = [f"<b>{title} · {now.strftime('%d.%m.%Y')} {TR_DAYS[now.weekday()]}</b>"]
    if comment:
        lines = comment.splitlines()
        L += [""] + [("⚠️ " + esc(x.removeprefix("Dikkat:").strip())) if x.startswith("Dikkat:") else f"💬 {esc(x)}" for x in lines]
    elif comment_note:
        L += ["", f"<i>{esc(comment_note)}</i>"]
    # market
    L += ["", "<b>📊 Piyasa</b>" + (" · son kapanış" if kind == "morning" else " · bugün")]
    parts = []
    for name, sym, v in d["market"]:
        if v.get("price") is None:
            continue
        if sym == "^TNX":
            parts.append(f"{name} %{tr_n(v['price'], 2)} ({_pct(v.get('change_pct'))})")
        elif sym in ("^VIX",):
            parts.append(f"{name} {tr_n(v['price'], 1)} ({_pct(v.get('change_pct'))})")
        else:
            parts.append(f"{name} {_pct(v.get('change_pct'))}")
    for i in range(0, len(parts), 3):
        L.append(" · ".join(parts[i:i + 3]))
    r = d.get("regime")
    if r:
        L.append(f"Rejim: <b>{esc(r['label'])}</b> {tr_n(r['overall'], 0)}/100 ({_d(r['day'])})")
    # portfolio
    pf = d.get("pf")
    if pf:
        line = f"Değer {tr_n(pf['value'], 0)}$ · gün {_arrow(pf['pnl'])} {tr_n(pf['pnl'], 0, True)}$ ({_pct(pf['pct'], 2)})"
        if pf.get("ytd") is not None:
            line += f" · yılbaşından {_pct(pf['ytd'])} (S&P {_pct(pf.get('ytd_spx'))})"
        L += ["", f"<b>💼 Portföy</b> · {esc(pf['basis'])}", line]
    L += karar_lines(d.get("karar"))
    # stocks
    held = [x for x in d["stocks"] if x["held"]]
    watch = [x for x in d["stocks"] if not x["held"]]
    if held:
        L += ["", "<b>📈 Hisselerim</b>"]
        for x in held:
            L.append(f"{_arrow(x['chg'])} <b>{x['symbol']}</b> {tr_n(x['price'], 2)}$ {_pct(x['chg'])}"
                     + (f" · ağırlık %{tr_n(x['weight'], 0)}" if x["weight"] is not None else ""))
            if x["event"]:
                e = x["event"]
                L.append(f"   └ {esc(_short(e['summary_tr'] or e['title']))} <i>(önem {e['imp']})</i>")
    movers = [x for x in watch if x["event"] or abs(x["chg"] or 0) >= 3]
    if movers:
        L += ["", "<b>👀 İzleme listesi</b>"]
        for x in movers[:6]:
            L.append(f"{_arrow(x['chg'])} <b>{x['symbol']}</b> {_pct(x['chg'])}"
                     + (f" — {esc(_short(x['event']['summary_tr'] or x['event']['title'], 120))}" if x["event"] else ""))
    if d["macro"]:
        L += ["", "<b>🏛 Makro ve politika</b>"]
        for e in d["macro"]:
            L.append(f"• {esc(_short(e['summary_tr'] or e['title']))}")
    st = d["setups"]
    if st["new"] or st["open"]:
        L += ["", f"<b>🎯 Setup'lar</b> · {_d(st['day'])} taraması"]
        for c in st["new"][:4]:
            L.append(f"• YENİ <b>{c['symbol']}</b> {esc(c['name_tr'])}: giriş {tr_n(c['entry_lo'])}–{tr_n(c['entry_hi'])}$, "
                     f"stop {tr_n(c['stop'])}$, H1 {tr_n(c['t1'])}$")
        for c in st["open"][:4]:
            L.append(f"• <b>{c['symbol']}</b> {esc(c['name_tr'])}: {esc(c['status_tr'])}")
    sj = d.get("strateji")
    if sj and (sj["new"] or sj["exits"]):
        L += ["", "<b>🧪 Strateji sinyalleri</b>" + (f" · {_d(sj['day'])} kapanışı" if sj["day"] else "") + " (kağıt defter)"]
        for t in sj["new"][:5]:
            L.append(f"• <b>{t['symbol']}</b> {esc(t['strategy_name'])}: "
                     + (f"limit alış {tr_n(t['limit_px'])}$ (yalnız bugün)" if t.get("limit_px") else "açılışta alış"))
        if len(sj["new"]) > 5:
            L.append(f"• +{len(sj['new']) - 5} sinyal daha")
        if sj["exits"]:
            L.append("Açılışta çıkış: " + ", ".join(f"<b>{t['symbol']}</b> ({_pct(t.get('ret'))})" for t in sj["exits"][:6]))
    if d["theses"] or d["alerts"]:
        L += ["", "<b>🧭 Tez ve alarmlar</b>"]
        for t in d["theses"][:5]:
            why = "; ".join(r["label"] for r in t["triggered"]) or f"gözden geçirme zamanı ({t['next_review']})"
            L.append(f"• <b>{t['symbol']}</b>: {esc(why)}")
        for a in d["alerts"][:5]:
            L.append(f"• 🔔 {esc(a['label'])}")
    if d["calendar"]:
        L += ["", "<b>🗓 Takvim</b> (36 saat, Berlin)"]
        for c in d["calendar"][:7]:
            L.append(f"• {fmt_local(c['release_at'], '%a %H:%M').replace('Mon', 'Pzt').replace('Tue', 'Sal').replace('Wed', 'Çar').replace('Thu', 'Per').replace('Fri', 'Cum').replace('Sat', 'Cmt').replace('Sun', 'Paz')} "
                     f"{esc(_short(c['name'], 90))}" + (" ⭐" if (c["importance"] or 0) >= 4 else ""))
    if d.get("fed"):
        f = d["fed"]
        L.append("Fed beklentisi (Kalshi): " + ", ".join(f"{esc(_fed_label(i['label']))} %{tr_n(i['prob'] * 100, 0)}" for i in f["items"]))
    L += ["", f"🔗 {config.PUBLIC_BASE_URL}/"]
    text = "\n".join(L)
    if len(text) > 4000:  # Telegram limit 4096
        text = text[:3990].rsplit("\n", 1)[0] + "\n…"
    return text


def facts_text(d: dict) -> str:
    """Compact plain text of the same numbers, for the model."""
    out = []
    for name, sym, v in d["market"]:
        if v.get("price") is not None:
            out.append(f"{name}: {tr_n(v['price'], 2)} ({_pct(v.get('change_pct'), 2)})")
    if d.get("pf"):
        out.append(f"Portföy günlük: {tr_n(d['pf']['pnl'] or 0, 0, True)}$ ({_pct(d['pf']['pct'], 2)})")
    if d.get("regime"):
        out.append(f"Rejim: {d['regime']['label']} {tr_n(d['regime']['overall'], 0)}")
    for x in d["stocks"]:
        if x["chg"] is not None:
            ev = x["event"]
            out.append(f"{x['symbol']}{' (portföy)' if x['held'] else ''}: {_pct(x['chg'], 2)}"
                       + (f" — {ev['summary_tr'] or ev['title']}" if ev else ""))
    for e in d["macro"]:
        out.append(f"Makro: {e['summary_tr'] or e['title']}")
    for c in d["calendar"][:6]:
        out.append(f"Takvim: {fmt_local(c['release_at'])} {c['name']}")
    return "\n".join(out)


def _clean_comment(text: str, facts: str) -> str | None:
    """Strip markdown; drop sentences carrying numbers that are not in the facts (number guard).
    Returns the main paragraph, then one 'Dikkat:' item per line."""
    from .scorecard import _numbers
    t = re.sub(r"[*#`_]{1,3}", "", text or "").strip()
    if not t or any(b in t for b in _BAD_REPLY):
        return None
    allowed = _numbers(facts)
    meta = ("doğrulayamad", "teyit edemed", "kaynak bulamad", "bulamadım")
    main, notes = [], []
    for line in t.splitlines():
        line = line.strip(" -•\t")
        if not line:
            continue
        for part in re.split(r"(?=Dikkat:)", line):
            part = part.strip()
            if not part:
                continue
            target = notes if part.startswith("Dikkat:") else main
            sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", part) if x.strip()]
            keep = [x for x in sents if not (_numbers(x) - allowed) and not any(m in x.lower() for m in meta)]
            if keep and target is notes and not keep[0].startswith("Dikkat:"):
                keep[0] = "Dikkat: " + keep[0]
            if keep:
                target.append(" ".join(keep))
    out = "\n".join([" ".join(main)] + notes[:2] if main else notes[:2]).strip()
    return out[:900] or None


def build(kind: str, *, use_llm: bool = True) -> str:
    d = gather(kind)
    facts = facts_text(d)
    comment, note = None, None
    if use_llm:
        focus = ("Gece ve sabah en önemli gelişme neydi, portföy için ne anlama geliyor?" if kind == "morning"
                 else "Piyasa bugün neden böyle kapandı (ana tema), portföy için ne anlama geliyor?")
        web = kind == "evening"
        web_rule = ("- Bugünkü hareketin nedenini web aramasıyla güvenilir kaynaklarda (Reuters, CNBC, Bloomberg, WSJ, AP) bul; "
                    "bulamazsan nedeni hiç anma, yalnızca verilerdeki gelişmelerden yorum yap.") if web else ""
        try:
            raw, _ = llm.complete(COMMENT_PROMPT.format(focus=focus, when="bugün" if kind == "morning" else "yarın", web_rule=web_rule, data=facts),
                                  toolsets="web" if web else "clarify", timeout=480 if web else 240, priority=True)
            comment = _clean_comment(raw, facts)
            if not comment:
                note = "Yorum üretilemedi (model yanıtı kullanılamadı); aşağıdaki veriler sunucu hesabıdır."
        except Exception as exc:  # noqa: BLE001
            log.error("digest LLM failed: %s", exc)
            note = "Yorum üretilemedi (" + ("günlük yapay zekâ limiti doldu" if "cap" in str(exc) else "model yanıt vermedi") + \
                   "); aşağıdaki veriler sunucu hesabıdır."
    return render(d, comment, note)


def send(kind: str, *, force: bool = False) -> bool:
    now = datetime.now(config.LOCAL_TZ)
    day = now.strftime("%Y-%m-%d")
    if not force and now.weekday() >= 5:
        return False
    if not force and db.query_one("SELECT 1 FROM digests WHERE kind = ? AND day = ?", (kind, day)):
        return False
    text = build(kind)
    db.execute(
        "INSERT INTO digests(kind, day, content, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(kind, day) DO UPDATE SET content = excluded.content, created_at = excluded.created_at",
        (kind, day, text, iso(utcnow())),
    )
    ok = notifier.send_now(text, kind="digest")
    if ok and kind == "morning":
        _mark_karar_sent()
    return ok


def due_digests() -> None:
    """Called every few minutes; sends a digest once its local time has passed (same day)."""
    now = datetime.now(config.LOCAL_TZ)
    for kind, key in (("morning", "digest_morning"), ("evening", "digest_evening")):
        h, m = map(int, db.get_setting(key).split(":"))
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= now < at + timedelta(hours=2):
            send(kind)


def reminders() -> int:
    """30 minutes before high-importance releases."""
    now = utcnow()
    rows = db.query(
        "SELECT * FROM macro_calendar WHERE reminded = 0 AND importance >= 4 AND release_at > ? AND release_at <= ?",
        (iso(now), iso(now + timedelta(minutes=35))),
    )
    for r in rows:
        when = parse_dt(r["release_at"])
        mins = int((when - now).total_seconds() // 60)
        notifier.enqueue(
            f"⏰ <b>{mins} dk sonra:</b> {esc(r['name'])} ({fmt_local(r['release_at'], '%H:%M')})\n"
            f"Açıklanınca sonucu ve piyasa tepkisini göndereceğim.",
            kind="reminder",
        )
        db.execute("UPDATE macro_calendar SET reminded = 1 WHERE id = ?", (r["id"],))
    return len(rows)
