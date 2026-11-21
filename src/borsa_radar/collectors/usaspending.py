"""USAspending.gov: federal contracts, grants and loans awarded to watchlist companies.

Data lags (agencies report with delay), so this is context rather than breaking news.
Recipient names are matched strictly ("^EOS ENERGY\\b") to avoid look-alikes (AEOS, SOFITC3...).
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
URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
GROUPS = {
    "sözleşme": (["A", "B", "C", "D"], "Award Amount", "Start Date"),
    "hibe": (["02", "03", "04", "05"], "Award Amount", "Start Date"),
    "kredi": (["07", "08"], "Loan Value", "Issued Date"),
}
MIN_AMOUNT = 1_000_000


def _importance(amount: float) -> int:
    return 4 if amount >= 100e6 else 3 if amount >= 10e6 else 2


def collect_ticker(ticker) -> int:
    kw = (keywords(ticker) or [ticker["symbol"]])[0]
    pattern = re.compile(rf"^{re.escape(kw.upper())}\b")
    first = db.get_cursor(f"usaspending:{ticker['symbol']}") is None
    new = 0
    for label, (codes, amount_field, date_field) in GROUPS.items():
        fields = ["Award ID", "Recipient Name", amount_field, "Awarding Agency", date_field, "Description",
                  "generated_internal_id", "Last Modified Date"]
        try:
            r = httpx.post(URL, timeout=60, json={
                "filters": {"recipient_search_text": [kw], "award_type_codes": codes,
                            "time_period": [{"start_date": str(date.today() - timedelta(days=730)), "end_date": str(date.today())}]},
                "fields": fields, "limit": 50, "sort": date_field, "order": "desc"})
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("usaspending %s/%s failed: %s", ticker["symbol"], label, exc)
            continue
        for a in results:
            name = (a.get("Recipient Name") or "").upper()
            amount = float(a.get(amount_field) or 0)
            if not pattern.search(name) or amount < MIN_AMOUNT:
                continue
            when = parse_dt(a.get(date_field)) or parse_dt(a.get("Last Modified Date")) or utcnow()
            seen_at = utcnow()
            status = "backfill" if first or when < utcnow() - timedelta(days=60) else "analyzed"
            agency = a.get("Awarding Agency") or "?"
            desc = (a.get("Description") or "").strip()
            title = f"{ticker['symbol']}: federal {label} — {agency}, {amount/1e6:,.1f}M$" + (f" ({desc[:80].lower()})" if desc else "")
            eid = db.insert_event(
                type="contract", source="USAspending", symbol=ticker["symbol"],
                url=f"https://www.usaspending.gov/award/{a.get('generated_internal_id')}",
                title=title, body=desc or None,
                # published = when we first saw it (data lags); award date kept in extra
                published_at=iso(seen_at if status == "analyzed" else when),
                dedupe_key=sha("usaspending", a.get("generated_internal_id") or a.get("Award ID") or title),
                status=status, importance=_importance(amount), direction=1,
                extra={"kind": label, "agency": agency, "amount": amount, "award_date": a.get(date_field),
                       "recipient": a.get("Recipient Name"), "award_id": a.get("Award ID")},
            )
            new += bool(eid and status == "analyzed")
    db.set_cursor(f"usaspending:{ticker['symbol']}", iso(utcnow()))
    return new


def collect() -> int:
    return sum(collect_ticker(t) for t in db.active_tickers())
