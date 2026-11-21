"""Deep research ("🔍 Derin bakış"): Hermes with web search on important events.

Flow: analyzed event (importance >= 4, fresh, alerted) → queued → Hermes `-t web` (Codex) →
findings + expectation + sources stored → follow-up Telegram message (respects quiet hours).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from . import config, db, llm, notifier
from .collectors import kalshi
from .notifier import esc
from .util import fmt_local, iso, utcnow, utcnow_iso

log = logging.getLogger(__name__)

MIN_IMPORTANCE = 4
MAX_AGE_HOURS = 6
STORY_HOURS = 48  # one deep look per story
TIMEOUT = 420

PROMPT = """Sen bir piyasa araştırmacısısın. Aşağıdaki gelişme için WEB'DE ARAŞTIRMA yap (web_search / web_extract araçlarını kullan),
güvenilir kaynaklardan (Reuters, Bloomberg, CNBC, WSJ, şirket açıklaması, SEC, Fed, BLS, analist notları) ek bağlam topla.

GELİŞME (#e{id}, {type}, {time} Berlin):
{title}
{summary}
Kaynak: {url}
İlgili hisseler: {symbols}
Kullanıcının izleme listesi: {watchlist}
{extra_ctx}

Bul ve yaz:
1. Ne oldu — kaynaklarda geçen ek detaylar ve rakamlar.
2. {expectation_task}
3. Neden önemli — piyasa / hisse için okuma (olumlu ve olumsuz senaryo).
4. Takip edilecekler — sıradaki tarih/katalizör.

KURALLAR: Sadece bulduğun bilgiye dayan; bulamadığını "bulunamadı" de. Rakamları kaynağıyla ver. Türkçe yaz.
Yanıtı SADECE şu JSON olarak ver:
{{"findings_tr": "madde madde düz metin (• ile), en fazla ~1200 karakter",
  "expectation_tr": "beklenti vs gerçekleşen tek cümle veya boş string",
  "sources": [{{"title": "...", "url": "https://..."}}]}}"""

EXPECT_DATA = ("Piyasa BEKLENTİSİ (konsensüs/medyan tahmin) neydi, gerçekleşen beklentinin üstünde mi altında mı? "
               "Sürprizin büyüklüğü ve ilk piyasa tepkisi.")
EXPECT_STOCK = ("Varsa beklenti karşılaştırması (analist tahmini, şirket rehberliği, konsensüs) ve analistlerin/piyasanın ilk tepkisi; "
                "analist not değişikliğiyse notun GEREKÇESİ.")


def enqueue_candidates() -> int:
    """Queue fresh, important, alerted events (daily cap)."""
    cap = db.get_int("research_daily_cap")
    day_start = datetime.now(config.LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.query_one("SELECT COUNT(*) n FROM research WHERE created_at >= ?", (iso(day_start),))["n"]
    if used >= cap:
        return 0
    rows = db.query(
        """SELECT e.* FROM events e
           WHERE e.status = 'analyzed' AND COALESCE(e.importance,0) >= ? AND e.published_at >= ?
             AND e.type IN ('news','rating','filing','macro','data','reg')
             AND COALESCE(json_extract(e.extra,'$.manual'),0) = 0
             AND EXISTS (SELECT 1 FROM notifications n WHERE n.event_id = e.id AND n.kind = 'alert' AND n.status != 'dropped')
             AND NOT EXISTS (SELECT 1 FROM research r WHERE r.event_id = e.id)
             AND NOT EXISTS (SELECT 1 FROM analyses a1 JOIN analyses a2 ON a2.story = a1.story AND a2.event_id != a1.event_id
                             JOIN research r2 ON r2.event_id = a2.event_id
                             WHERE a1.event_id = e.id AND a1.story IS NOT NULL AND r2.created_at >= ?)
           ORDER BY e.importance DESC, e.published_at DESC LIMIT ?""",
        (MIN_IMPORTANCE, iso(utcnow() - timedelta(hours=MAX_AGE_HOURS)), iso(utcnow() - timedelta(hours=STORY_HOURS)), cap - used),
    )
    for e in rows:
        request(e["id"])
    return len(rows)


def request(event_id: int) -> None:
    """Queue (or re-queue) research for an event — also used by the UI button."""
    db.execute(
        """INSERT INTO research(event_id, status, created_at) VALUES (?, 'queued', ?)
           ON CONFLICT(event_id) DO UPDATE SET status = 'queued', error = NULL""",
        (event_id, utcnow_iso()),
    )


def _prompt(e) -> str:
    a = db.query_one("SELECT summary_tr FROM analyses WHERE event_id = ?", (e["id"],))
    syms = db.event_symbols(e["id"])
    extra_ctx = ""
    if e["type"] in ("data", "macro"):
        k = kalshi.context_text()
        if k:
            extra_ctx = "Kalshi tahmin piyasası (güncel olasılıklar):\n" + k
    return PROMPT.format(
        id=e["id"], type=e["type"], time=fmt_local(e["published_at"], "%Y-%m-%d %H:%M"), title=e["title"],
        summary=(a["summary_tr"] if a and a["summary_tr"] else (e["body"] or "")[:800]), url=e["url"] or "-",
        symbols=", ".join(syms) or "-", watchlist=", ".join(t["symbol"] for t in db.active_tickers()),
        extra_ctx=extra_ctx, expectation_task=EXPECT_DATA if e["type"] == "data" else EXPECT_STOCK,
    )


_MD_LINK_PAREN = re.compile(r"\(\[([^\]]+)\]\((https?://[^)]+)\)\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def clean(text: str) -> str:
    """'([reuters.com](https://...))' → '(reuters.com)' — sources are listed separately."""
    text = _MD_LINK_PAREN.sub(r"(\1)", text or "")
    return _MD_LINK.sub(r"\1", text).strip()


def _clean_url(url: str) -> str:
    return re.sub(r"[?&]utm_source=openai$", "", url)


def run_one() -> bool:
    row = db.query_one("SELECT * FROM research WHERE status = 'queued' ORDER BY created_at LIMIT 1")
    if not row:
        return False
    e = db.query_one("SELECT * FROM events WHERE id = ?", (row["event_id"],))
    if not e:
        db.execute("DELETE FROM research WHERE event_id = ?", (row["event_id"],))
        return True
    db.execute("UPDATE research SET status = 'running' WHERE event_id = ?", (e["id"],))
    try:
        data, model = llm.complete_json(_prompt(e), timeout=TIMEOUT, toolsets="web")
        if isinstance(data, list):
            data = data[0] if data else {}
        sources = [{"title": s.get("title"), "url": _clean_url(str(s["url"]))}
                   for s in (data.get("sources") or []) if isinstance(s, dict) and str(s.get("url", "")).startswith("http")][:8]
        data["findings_tr"] = clean(data.get("findings_tr") or "")
        data["expectation_tr"] = clean(data.get("expectation_tr") or "")
        db.execute(
            """UPDATE research SET status = 'done', content = ?, expectation = ?, sources = ?, model = ?, done_at = ?
               WHERE event_id = ?""",
            (data.get("findings_tr") or "", data.get("expectation_tr") or "", json.dumps(sources, ensure_ascii=False),
             model, utcnow_iso(), e["id"]),
        )
        _notify(e, data, sources)
    except llm.CapReached as exc:
        log.info("research #%s deferred: %s", e["id"], exc)
        db.execute("UPDATE research SET status = 'queued' WHERE event_id = ?", (e["id"],))
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("research #%s failed: %s", e["id"], exc)
        db.execute("UPDATE research SET status = 'failed', error = ?, done_at = ? WHERE event_id = ?",
                   (str(exc)[:500], utcnow_iso(), e["id"]))
    return True


def _notify(e, data: dict, sources: list[dict]) -> None:
    if json.loads(e["extra"] or "{}").get("manual"):
        return
    syms = " ".join(db.event_symbols(e["id"])) or ("MAKRO" if e["type"] in ("macro", "data", "reg") else "")
    head = notifier.headline(e)
    head = head if len(head) <= 160 else head[:159].rsplit(" ", 1)[0] + "…"
    # sent as a reply under its alert (notifier.flush), so keep the header short
    lines = [f"🔍 <b>Derin bakış · {esc(syms)}</b>", f"<i>{esc(head)}</i>"]
    if data.get("expectation_tr"):
        lines.append(f"🎯 {esc(data['expectation_tr'])}")
    if data.get("findings_tr"):
        lines.append(esc(data["findings_tr"][:1800]))
    if sources:
        lines.append("Kaynaklar: " + " · ".join(
            f"<a href=\"{esc(s['url'])}\">{esc((s.get('title') or 'kaynak')[:40])}</a>" for s in sources[:4]))
    lines.append(f"🔗 <a href=\"{notifier.event_url(e['id'])}\">detay</a>")
    notifier.enqueue("\n".join(lines), kind="research", event_id=e["id"])


def get(event_id: int):
    r = db.query_one("SELECT * FROM research WHERE event_id = ?", (event_id,))
    if not r:
        return None
    d = dict(r)
    d["sources"] = db.loads(r["sources"], [])
    return d
