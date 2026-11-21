"""Per-ticker news: Yahoo RSS, Yahoo news API, Google News RSS."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import json

import feedparser
import httpx

from .. import config, db, market
from ..util import (
    iso, jaccard, normalize_url, parse_dt, sha, strip_html, strip_source_suffix, title_key,
    title_tokens, utcnow,
)

log = logging.getLogger(__name__)

_SUFFIXES = {
    "inc", "inc.", "incorporated", "corp", "corp.", "corporation", "co", "co.", "company", "ltd", "ltd.",
    "plc", "holdings", "holding", "group", "technologies", "technology", "enterprises", "systems",
    "the", "class", "a", "sa", "nv", "ag", "&",
}

FRESH_HOURS = 48          # older items are stored as backfill (no alert)
NEAR_DUP = 0.7            # title Jaccard threshold within the same symbol


def keywords(ticker) -> list[str]:
    """Company-name keywords used to filter noisy feeds, e.g. 'Eos Energy', 'Micron'."""
    name = (ticker["short_name"] or ticker["name"] or "").replace(",", " ")
    words = [w for w in name.split() if w.lower() not in _SUFFIXES]
    kws = []
    if words:
        kws.append(" ".join(words[:2]))
        if len(words) > 1 and len(words[0]) >= 4:
            kws.append(words[0])
    return kws


def mentions(ticker, text: str) -> bool:
    sym = ticker["symbol"]
    if len(sym) >= 3 and re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", text):
        return True
    if re.search(rf"(\$|\(|:\s?){re.escape(sym)}\b", text):
        return True
    return any(re.search(rf"\b{re.escape(k)}\b", text, re.I) for k in keywords(ticker))


def _items_yahoo_rss(sym: str) -> list[dict]:
    f = feedparser.parse(
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US",
        agent=config.BROWSER_UA,
    )
    return [
        {
            "source": "yahoo",
            "title": e.get("title", ""),
            "url": e.get("link"),
            "body": strip_html(e.get("summary"), 1500),
            "published": parse_dt(e.get("published_parsed") or e.get("published")),
        }
        for e in f.entries
    ]


def _items_yahoo_api(sym: str) -> list[dict]:
    out = []
    for n in market.yahoo_news(sym):
        c = n.get("content") or n
        if c.get("contentType") not in (None, "STORY"):
            continue  # skip videos
        tickers = {t.get("symbol") for t in ((c.get("finance") or {}).get("stockTickers") or [])}
        url = ((c.get("canonicalUrl") or {}).get("url")) or ((c.get("clickThroughUrl") or {}).get("url"))
        out.append({
            "source": "yahoo",
            "title": c.get("title", ""),
            "url": url,
            "body": strip_html(c.get("summary") or c.get("description"), 1500),
            "published": parse_dt(c.get("pubDate")),
            "tagged": sym in tickers,
            "publisher": (c.get("provider") or {}).get("displayName"),
        })
    return out


def _items_google(ticker) -> list[dict]:
    kws = keywords(ticker) or [ticker["symbol"]]
    q = quote_plus(f'"{kws[0]}" stock when:2d')
    f = feedparser.parse(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", agent=config.BROWSER_UA)
    out = []
    for e in f.entries:
        src = (e.get("source") or {}).get("title")
        out.append({
            "source": "google",
            "title": strip_source_suffix(e.get("title", "")),
            "url": e.get("link"),
            "body": "",
            "published": parse_dt(e.get("published_parsed") or e.get("published")),
            "publisher": src,
        })
    return out


def _items_finnhub(sym: str) -> list[dict]:
    key = config.secret("FINNHUB_API_KEY")
    if not key:
        return []
    today = utcnow().date()
    r = httpx.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": sym, "from": str(today - timedelta(days=3)), "to": str(today), "token": key},
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for n in r.json() if isinstance(r.json(), list) else []:
        ts = n.get("datetime")
        out.append({
            "source": "finnhub",
            "title": n.get("headline", ""),
            "url": n.get("url"),
            "body": strip_html(n.get("summary"), 1500),
            "published": datetime.fromtimestamp(ts, timezone.utc) if ts else None,
            "tagged": sym in (n.get("related") or "").split(","),
            "publisher": n.get("source"),
        })
    return out


def collect_marketaux() -> int:
    """One request for the whole watchlist (free plan: 100 req/day, 3 articles/request).
    Articles carry per-entity sentiment scores (-1..1)."""
    key = config.secret("MARKETAUX_API_KEY")
    tickers = {t["symbol"]: t for t in db.active_tickers()}
    if not key or not tickers:
        return 0
    since = db.get_cursor("marketaux_since") or iso(utcnow() - timedelta(hours=6))
    r = httpx.get("https://api.marketaux.com/v1/news/all", timeout=30, params={
        "symbols": ",".join(tickers), "filter_entities": "true", "must_have_entities": "true",
        "language": "en", "published_after": since[:16], "sort": "published_at", "api_token": key})
    r.raise_for_status()
    new = 0
    latest = since
    for a in r.json().get("data") or []:
        pub = parse_dt(a.get("published_at"))
        if not pub:
            continue
        latest = max(latest, iso(pub))
        ents = [e for e in a.get("entities") or [] if e.get("symbol") in tickers]
        sentiment = {e["symbol"]: e.get("sentiment_score") for e in ents}
        for e in ents:
            t = tickers[e["symbol"]]
            title = (a.get("title") or "").strip()
            if not title:
                continue
            toks = title_tokens(title)
            dup = next((eid for eid, tt in _recent_title_sets(t["symbol"]) if jaccard(toks, tt) >= NEAR_DUP), None)
            if dup:
                db.execute("UPDATE events SET extra = json_set(COALESCE(extra,'{}'), '$.sentiment', json(?)) WHERE id = ?",
                           (json.dumps(sentiment), dup))
                continue
            status = "backfill" if t["backfilled_at"] is None else "pending"
            eid = db.insert_event(
                type="news", source=a.get("source") or "marketaux", symbol=t["symbol"], symbols=list(sentiment),
                url=a.get("url"), title=title, body=strip_html(a.get("description") or a.get("snippet"), 1500) or None,
                published_at=iso(pub), dedupe_key=sha("news", normalize_url(a.get("url")) or title_key(title)),
                title_key=title_key(title), status=status, extra={"sentiment": sentiment},
            )
            new += bool(eid and status == "pending")
            break  # one event per article; other symbols attached via `symbols`
    db.set_cursor("marketaux_since", latest)
    return new


def _recent_title_sets(sym: str) -> list[tuple[int, set[str]]]:
    rows = db.query(
        """SELECT e.id, e.title FROM events e JOIN event_symbols s ON s.event_id = e.id
           WHERE s.symbol = ? AND e.type = 'news' AND e.published_at >= ?""",
        (sym, iso(utcnow() - timedelta(hours=96))),
    )
    return [(r["id"], title_tokens(r["title"])) for r in rows]


def collect_ticker(ticker) -> int:
    """Fetch, filter, dedupe and store news for one ticker. Returns # new pending events."""
    sym = ticker["symbol"]
    backfill = ticker["backfilled_at"] is None
    items: list[dict] = []
    for fn in (lambda: _items_finnhub(sym), lambda: _items_yahoo_rss(sym), lambda: _items_yahoo_api(sym),
               lambda: _items_google(ticker)):
        try:
            items.extend(fn())
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            log.warning("news source failed for %s: %s", sym, exc)

    recent = _recent_title_sets(sym)
    fresh_cut = utcnow() - timedelta(hours=FRESH_HOURS)
    new_pending = 0
    for it in items:
        title = (it.get("title") or "").strip()
        if not title or not it.get("published"):
            continue
        text = f"{title} {it.get('body') or ''}"
        if not it.get("tagged") and not mentions(ticker, text):
            continue
        url = normalize_url(it.get("url"))
        toks = title_tokens(title)
        dup_of = next((eid for eid, t in recent if jaccard(toks, t) >= NEAR_DUP), None)
        if dup_of:
            db.execute("INSERT OR IGNORE INTO event_symbols(event_id, symbol) VALUES (?, ?)", (dup_of, sym))
            continue
        old = it["published"] < fresh_cut
        status = "backfill" if (backfill or old) else "pending"
        eid = db.insert_event(
            type="news",
            source=it.get("publisher") or it["source"],
            symbol=sym,
            url=it.get("url"),
            title=title,
            body=it.get("body") or None,
            published_at=iso(it["published"]),
            dedupe_key=sha("news", url or title_key(title)),
            title_key=title_key(title),
            status=status,
        )
        if eid:
            recent.append((eid, toks))
            if status == "pending":
                new_pending += 1
    return new_pending
