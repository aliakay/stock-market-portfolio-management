"""Federal Register: executive orders, export controls, tariffs, bank/consumer-finance, energy rules.

Pre-filtered by agency/type here; the LLM (macro prompt, with portfolio impact) decides relevance.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import httpx

from .. import db
from ..util import iso, parse_dt, sha, utcnow
from .news import keywords

log = logging.getLogger(__name__)
BASE = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["title", "type", "subtype", "publication_date", "html_url", "abstract", "agencies", "document_number", "action",
          "significant", "effective_on"]

# (agencies, document types)
QUERIES = [
    (["industry-and-security-bureau", "trade-representative-office-of-united-states", "consumer-financial-protection-bureau"],
     ["RULE", "PRORULE", "NOTICE"]),
    (["international-trade-administration", "treasury-department", "federal-reserve-system", "comptroller-of-the-currency",
      "federal-deposit-insurance-corporation", "securities-and-exchange-commission", "energy-department",
      "federal-energy-regulatory-commission", "education-department"],
     ["RULE", "PRORULE"]),
]
NOISE = re.compile(r"\b(week|day|month|anniversary|honoring|in memory|national .* (day|month|week))\b", re.I)
ROUTINE = re.compile(r"(continuation of the national emergency|sunshine act|agency information collection|privacy act of 1974|"
                     r"meeting(s)? (notice|of)|submission for omb review|request for nominations)", re.I)


def _fetch(params: dict) -> list[dict]:
    params = {**params, "per_page": 200, "order": "newest", "fields[]": FIELDS}
    r = httpx.get(BASE, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("results") or []


def collect() -> int:
    since_s = db.get_cursor("fedreg_since")
    first_run = since_s is None
    since = date.fromisoformat(since_s) if since_s else date.today() - timedelta(days=3)
    docs: dict[str, dict] = {}
    try:
        for d in _fetch({"conditions[publication_date][gte]": str(since), "conditions[type][]": ["PRESDOCU"]}):
            if d.get("subtype") == "Proclamation" and NOISE.search(d["title"]):
                continue
            docs[d["document_number"]] = d
        for agencies, types in QUERIES:
            for d in _fetch({"conditions[publication_date][gte]": str(since), "conditions[agencies][]": agencies,
                             "conditions[type][]": types}):
                docs.setdefault(d["document_number"], d)
        # direct mentions of watchlist companies anywhere
        for t in db.active_tickers():
            kw = (keywords(t) or [t["symbol"]])[0]
            for d in _fetch({"conditions[publication_date][gte]": str(since), "conditions[term]": f'"{kw}"'}):
                d["_mention"] = t["symbol"]
                docs.setdefault(d["document_number"], d)
    except Exception as exc:  # noqa: BLE001
        log.warning("federal register failed: %s", exc)
        return 0

    new = 0
    for num, d in docs.items():
        if ROUTINE.search(d["title"]) and not d.get("_mention"):
            continue
        pub = parse_dt(d["publication_date"] + "T10:00:00-05:00")  # FR publishes ~ morning ET
        agencies = ", ".join(a.get("name", "") for a in d.get("agencies") or [] if a.get("name"))
        kind = d.get("subtype") or {"RULE": "Kural", "PRORULE": "Taslak kural", "NOTICE": "Duyuru", "PRESDOCU": "Başkanlık belgesi"}.get(d["type"], d["type"])
        status = "backfill" if first_run or pub < utcnow() - timedelta(days=2) else "pending"
        eid = db.insert_event(
            type="reg", source=f"Federal Register · {agencies[:60]}", url=d.get("html_url"),
            title=f"🏛️ {kind}: {d['title']}", body=(d.get("abstract") or d.get("action") or "")[:2000] or None,
            published_at=iso(pub), dedupe_key=sha("fedreg", num), status=status,
            symbol=d.get("_mention"),
            extra={"document_number": num, "type": d["type"], "subtype": d.get("subtype"), "agencies": agencies,
                   "effective_on": d.get("effective_on"), "significant": d.get("significant")},
        )
        new += bool(eid and status == "pending")
    db.set_cursor("fedreg_since", str(date.today() - timedelta(days=1)))
    return new
