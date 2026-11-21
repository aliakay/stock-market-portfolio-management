"""LLM analysis of pending events (batched)."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from . import db, llm, market
from .util import fmt_local, iso, parse_dt, utcnow, utcnow_iso

log = logging.getLogger(__name__)

BATCH = 10
MAX_WAIT_MIN = 15      # a non-portfolio batch is sent when full or when its oldest item has waited this long
HELD_WAIT_MIN = 10     # same for portfolio news / filings
EXPIRE_HOURS = 24      # pending items older than this become 'backfill' (analysable on demand)
_OPTION_PAGE = re.compile(r"\b[A-Z]{1,6}\d{6}[CP]\d{5,8}\b|interactive stock chart|stock options chain", re.I)
_JUNK_SOURCES = {"stock traders daily"}

STOCK_PROMPT = """You are a sharp equity analyst. Analyze these new items about US stocks the user holds/watches.
Watchlist: {watchlist}
Current quotes: {quotes}

For EACH item return an object:
- "id": item id (int)
- "relevant": false if it is not really about the company (generic market list, unrelated, pure clickbait, duplicate rehash) else true
- "summary_tr": 1-2 sentence factual summary in Turkish (what happened, key numbers)
- "reasoning_tr": 1-2 sentences in Turkish: why it matters / likely market reaction
- "category": one of earnings|guidance|analyst|product|contract|partnership|m&a|financing|insider|legal|regulatory|management|macro|opinion|other
- "direction": -1 (negative for the stock), 0 (neutral/unclear), 1 (positive)
- "importance": 1-5. Calibrate strictly:
   5 = thesis-changing (earnings shock, M&A, bankruptcy/dilution shock, major guidance cut/raise, fraud)
   4 = clearly price-moving company-specific news (big contract, offering priced, rating change by major bank, key exec exit, FDA/DOE-type decision)
   3 = notable but moderate (new product/partnership, conference remarks with new info, sector news directly hitting it)
   2 = minor / incremental / mostly priced in
   1 = noise: opinion pieces, "is X a buy?", listicles, price-move recaps without new info
- "affected_symbols": list of watchlist tickers affected (subset of watchlist)
- "story": the story this item belongs to. Reuse a KNOWN STORY key when it is the same underlying event or topic
   (follow-ups, other outlets repeating it, reactions, previews of the same thing); items in this batch about the same
   event must share one key. Otherwise create a new key: lowercase, hyphenated, 2-6 words, starting with the main
   ticker or topic (e.g. "nvda-ceo-huang-exit", "fed-december-hike-odds").
- "new_info": true only if the item adds materially new facts to a KNOWN story (new numbers, a decision, an official
   statement, a confirmed deal); false for rehashes, opinion, other outlets repeating it. Always true for a new story.

KNOWN STORIES (last 3 days; key — latest summary):
{stories}

Items:
{items}

Reply with ONLY a JSON array, no prose."""

MACRO_PROMPT = """You are a macro strategist covering the US economy and markets.
The user's watchlist: {watchlist} ({names})

For EACH item return an object:
- "id": item id (int)
- "relevant": false unless it concerns the US economy, Fed/monetary policy, fiscal/trade policy (tariffs, budget), inflation/jobs/growth data, rates/dollar/oil, financial stability, major geopolitical events moving US markets, OR (for type=reg Federal Register documents) a rule/order that materially affects a watchlist company or its sector (export controls on chips/AI, bank/consumer-lending rules, student loans, energy storage/grid policy, tariffs). Routine administrative notices, personal finance advice, lifestyle pieces => false.
- "summary_tr": 1-2 sentence factual summary in Turkish with key numbers
- "reasoning_tr": 2-3 sentences in Turkish: what it means for US stocks/rates/dollar (risk-on or risk-off, rate-cut odds, sectors)
- "category": one of fed|inflation|jobs|growth|consumer|housing|trade|fiscal|rates|energy|geopolitics|markets|other
- "direction": market-wide effect for US equities: -1 negative, 0 neutral, 1 positive
- "importance": 1-5. 5 = FOMC decision / CPI or NFP surprise / shock event; 4 = clearly market-moving (Fed chair remarks with new signal, big data surprise, tariff escalation); 3 = notable; 2 = minor; 1 = noise/not relevant
- "portfolio_impact": object mapping EVERY watchlist ticker to -1/0/1 (likely effect on that stock; consider rate sensitivity, AI/semis exposure, growth vs value, energy/storage policy)
- "portfolio_note_tr": one short Turkish sentence explaining the portfolio effect
- "story": the story this item belongs to. Reuse a KNOWN STORY key when it is the same underlying event or topic
   (follow-ups, other outlets repeating it, reactions, previews of the same thing); items in this batch about the same
   event must share one key. Otherwise create a new key: lowercase, hyphenated, 2-6 words, starting with the main
   ticker or topic (e.g. "nvda-ceo-huang-exit", "fed-december-hike-odds").
- "new_info": true only if the item adds materially new facts to a KNOWN story (new numbers, a decision, an official
   statement, a confirmed deal); false for rehashes, opinion, other outlets repeating it. Always true for a new story.

KNOWN STORIES (last 3 days; key — latest summary):
{stories}

Items:
{items}

Reply with ONLY a JSON array, no prose."""


def _item_text(e) -> str:
    syms = db.event_symbols(e["id"])
    extra = db.loads(e["extra"], {})
    lines = [
        f"[id={e['id']}] type={e['type']} source={e['source']} time={fmt_local(e['published_at'], '%Y-%m-%d %H:%M')} (Berlin)",
        f"symbols: {', '.join(syms) or '-'}",
        f"title: {e['title']}",
    ]
    if e["body"]:
        lines.append(f"text: {e['body'][:2500]}")
    if extra:
        lines.append(f"meta: {json.dumps(extra, ensure_ascii=False)[:600]}")
    return "\n".join(lines)


def _watch():
    t = db.active_tickers()
    return [r["symbol"] for r in t], ", ".join(f"{r['symbol']}={r['short_name'] or r['name']}" for r in t)


def _known_stories(events, *, macro: bool, hours: int = 72, limit: int = 30) -> str:
    since = iso(utcnow() - timedelta(hours=hours))
    if macro:
        rows = db.query("""SELECT a.story, a.summary_tr FROM analyses a JOIN events e ON e.id = a.event_id
                           WHERE a.story IS NOT NULL AND e.published_at >= ? AND e.type IN ('macro','data','reg')
                           ORDER BY e.published_at DESC""", (since,))
    else:
        syms = sorted({s for e in events for s in db.event_symbols(e["id"])})
        if not syms:
            return "-"
        rows = db.query(f"""SELECT a.story, a.summary_tr FROM analyses a JOIN events e ON e.id = a.event_id
                            WHERE a.story IS NOT NULL AND e.published_at >= ? AND e.type IN ('news','filing')
                              AND EXISTS (SELECT 1 FROM event_symbols s WHERE s.event_id = e.id AND s.symbol IN ({",".join("?" * len(syms))}))
                            ORDER BY e.published_at DESC""", (since, *syms))
    seen: dict[str, str] = {}
    for r in rows:
        if r["story"] not in seen:
            seen[r["story"]] = (r["summary_tr"] or "")[:160]
        if len(seen) >= limit:
            break
    return "\n".join(f"- {k} — {v}" for k, v in seen.items()) or "-"


def _story_key(v) -> str | None:
    k = re.sub(r"[^a-z0-9]+", "-", str(v or "").lower()).strip("-")[:60]
    return k or None


def _store(e, res: dict, model: str, *, macro: bool) -> None:
    imp = int(res.get("importance") or 1)
    imp = max(1, min(5, imp))
    if e["importance"] and e["type"] in ("filing", "data"):
        imp = max(imp, int(e["importance"]))  # rule-based floor for official sources
    direction = int(res.get("direction") or 0)
    direction = max(-1, min(1, direction))
    relevant = bool(res.get("relevant", True))
    impact = res.get("portfolio_impact") if macro else None
    reasoning = res.get("reasoning_tr") or ""
    if macro and res.get("portfolio_note_tr"):
        reasoning = f"{reasoning}\nPortföy: {res['portfolio_note_tr']}".strip()
    with db.tx() as c:
        c.execute(
            """INSERT OR REPLACE INTO analyses(event_id, summary_tr, reasoning_tr, category, direction, importance,
                                               portfolio_impact, model, created_at, story, new_info)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (e["id"], res.get("summary_tr"), reasoning, res.get("category"), direction, imp,
             json.dumps(impact) if impact else None, model, utcnow_iso(), _story_key(res.get("story")),
             0 if res.get("new_info") is False else 1),
        )
        c.execute(
            "UPDATE events SET status = ?, importance = ?, direction = ? WHERE id = ?",
            ("analyzed" if relevant else "irrelevant", imp if relevant else 1, direction, e["id"]),
        )
        for s in res.get("affected_symbols") or []:
            if isinstance(s, str) and s.upper() in {r["symbol"] for r in db.active_tickers()}:
                c.execute("INSERT OR IGNORE INTO event_symbols(event_id, symbol) VALUES (?, ?)", (e["id"], s.upper()))


def _run_batch(events, *, macro: bool, budget: str = "daily") -> int:
    syms, names = _watch()
    items = "\n\n".join(_item_text(e) for e in events)
    if macro:
        prompt = MACRO_PROMPT.format(watchlist=", ".join(syms), names=names, items=items, stories=_known_stories(events, macro=True))
    else:
        q = market.quotes(sorted({s for e in events for s in db.event_symbols(e["id"])}))
        quotes = ", ".join(
            f"{s} {v['price']:.2f}$ ({v['change_pct']:+.1f}%)" for s, v in q.items() if v.get("price") and v.get("change_pct") is not None
        )
        prompt = STOCK_PROMPT.format(watchlist=names, quotes=quotes or "-", items=items, stories=_known_stories(events, macro=False))
    data, model = llm.complete_json(prompt, budget=budget)
    if isinstance(data, dict):
        data = data.get("items") or [data]
    by_id = {int(d["id"]): d for d in data if isinstance(d, dict) and str(d.get("id", "")).isdigit()}
    done = 0
    for e in events:
        res = by_id.get(e["id"])
        if res:
            _store(e, res, model, macro=macro)
            done += 1
    return done


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def prefilter() -> int:
    """Rule-based triage before the model: option-chain pages, known boilerplate sources, repeated headlines."""
    rows = db.query("SELECT id, type, source, title, published_at FROM events WHERE status = 'pending'")
    if not rows:
        return 0
    since = iso(utcnow() - timedelta(days=3))
    seen = {_norm_title(r["title"]) for r in db.query(
        "SELECT title FROM events WHERE status IN ('analyzed','irrelevant','backfill') AND type = 'news' AND created_at >= ?", (since,))}
    n = 0
    for r in rows:
        why = None
        if r["type"] == "news":
            key = _norm_title(r["title"])
            if _OPTION_PAGE.search(r["title"] or ""):
                why = "option_page"
            elif (r["source"] or "").lower() in _JUNK_SOURCES:
                why = "boilerplate"
            elif key and key in seen:
                db.execute("UPDATE events SET status = 'duplicate' WHERE id = ?", (r["id"],))
                n += 1
                continue
            seen.add(key)
        if why:
            db.execute("UPDATE events SET status = 'irrelevant', importance = 1, "
                       "extra = json_set(COALESCE(extra,'{}'), '$.prefilter', ?) WHERE id = ?", (why, r["id"]))
            n += 1
    old = db.execute("UPDATE events SET status = 'backfill' WHERE status = 'pending' AND published_at < ? "
                     "AND COALESCE(json_extract(extra,'$.manual'),0) = 0", (iso(utcnow() - timedelta(hours=EXPIRE_HOURS)),))
    return n + (old or 0)


def _held() -> set[str]:
    try:
        from . import portfolio
        return {p["symbol"] for p in portfolio.positions()}
    except Exception:  # noqa: BLE001
        return set()


def _ready(batch, wait_min: int, now) -> bool:
    """Full batch, or its oldest item waited long enough, or a rule already marked something important."""
    oldest = min(parse_dt(e["created_at"]) or now for e in batch)
    return len(batch) >= BATCH or (now - oldest).total_seconds() >= wait_min * 60 or any((e["importance"] or 0) >= 4 for e in batch)


def _queue() -> list[tuple[bool, list, str]]:
    """Batches to send now, most important first: (macro?, events, budget).
    Portfolio news and filings wait at most HELD_WAIT_MIN and may run ahead of the pace ("held");
    the rest waits for a full batch (or MAX_WAIT_MIN, or rule importance ≥ 4) and follows the pace."""
    rows = db.query("SELECT * FROM events WHERE status = 'pending' ORDER BY published_at DESC LIMIT 200")
    held = _held()
    now = utcnow()
    out = []
    for macro in (False, True):
        evs = [r for r in rows if r["type"] in (("macro", "data", "reg") if macro else ("news", "filing"))]
        if not macro:
            mine = [e for e in evs if held & set(db.event_symbols(e["id"])) or e["type"] == "filing"]
            rest = [e for e in evs if e not in mine]
            for i in range(0, len(mine), BATCH):  # portfolio news and filings: shorter wait, may run ahead of the pace
                if _ready(mine[i:i + BATCH], HELD_WAIT_MIN, now):
                    out.append((False, mine[i:i + BATCH], "held"))
            evs = rest
        else:
            evs.sort(key=lambda e: -(e["importance"] or 0))
        for i in range(0, len(evs), BATCH):
            if _ready(evs[i:i + BATCH], MAX_WAIT_MIN, now):
                out.append((macro, evs[i:i + BATCH], "paced"))
    order = {"held": 0, "paced": 1}
    return sorted(out, key=lambda x: order[x[2]])


def analyze_pending(limit: int = 6) -> int:
    """Send up to `limit` batches. Budget exhaustion leaves items pending (no failure count)."""
    prefilter()
    total = 0
    for macro, batch, budget in _queue()[:limit]:
        try:
            total += _run_batch(batch, macro=macro, budget=budget)
        except llm.CapReached as exc:
            log.info("analysis deferred (%s): %d items stay queued", exc, len(batch))
            if budget == "paced":
                break
            continue
        except Exception as exc:  # noqa: BLE001
            log.error("analysis batch failed: %s", exc)
            # don't retry forever: give up on items after repeated real failures
            for e in batch:
                db.execute(
                    "UPDATE events SET extra = json_set(COALESCE(extra,'{}'), '$.llm_fail', COALESCE(json_extract(extra,'$.llm_fail'),0)+1) WHERE id = ?",
                    (e["id"],),
                )
                row = db.query_one("SELECT json_extract(extra,'$.llm_fail') AS n FROM events WHERE id = ?", (e["id"],))
                if row and (row["n"] or 0) >= 3:
                    db.execute("UPDATE events SET status = 'backfill' WHERE id = ?", (e["id"],))
    return total


def analyze_one(event_id: int) -> bool:
    """On-demand analysis (UI button) for backfill items."""
    e = db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    if not e or e["type"] in ("rating", "flow", "contract"):
        return False
    # manual = analyzed on request; never triggers a Telegram alert
    db.execute(
        "UPDATE events SET status = 'pending', extra = json_set(COALESCE(extra,'{}'), '$.manual', 1) WHERE id = ?",
        (event_id,),
    )
    e = db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    return _run_batch([e], macro=e["type"] in ("macro", "data", "reg")) == 1
