"""Positioning & attention: FINRA short volume (daily), FINRA short interest (bi-monthly), Reddit mentions (ApeWisdom)."""

from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta

import httpx

from .. import db
from ..util import iso, sha, utcnow

log = logging.getLogger(__name__)

# ── FINRA daily short sale volume (Reg SHO, consolidated NMS) ─────────────

SV_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
SV_SPIKE = 0.15          # ratio above 20-day average (absolute points)


def collect_short_volume(days_back: int = 7) -> int:
    syms = {t["symbol"] for t in db.active_tickers()}
    loaded = 0
    for i in range(days_back, -1, -1):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5 or db.get_cursor(f"finra_sv:{d}") == "1":
            continue
        try:
            r = httpx.get(SV_URL.format(d=d), timeout=60)
        except httpx.HTTPError as exc:
            log.warning("finra short volume %s failed: %s", d, exc)
            continue
        if r.status_code == 404:
            continue  # not published yet / holiday
        r.raise_for_status()
        for line in r.text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) < 5 or parts[1] not in syms:
                continue
            short, total = float(parts[2]), float(parts[4])
            db.execute(
                "INSERT OR REPLACE INTO short_volume(symbol, day, short_vol, total_vol, ratio) VALUES (?,?,?,?,?)",
                (parts[1], str(d), short, total, short / total if total else None),
            )
        db.set_cursor(f"finra_sv:{d}", "1")
        loaded += 1
        _short_volume_spikes(str(d))
    return loaded


def _short_volume_spikes(day: str) -> None:
    """Spike vs 20-day average → low-importance 'flow' event (UI/digest only, below alert threshold)."""
    for row in db.query("SELECT * FROM short_volume WHERE day = ?", (day,)):
        hist = [r["ratio"] for r in db.query(
            "SELECT ratio FROM short_volume WHERE symbol = ? AND day < ? AND ratio IS NOT NULL ORDER BY day DESC LIMIT 20",
            (row["symbol"], day))]
        if len(hist) < 10 or row["ratio"] is None:
            continue
        avg = statistics.mean(hist)
        if row["ratio"] - avg >= SV_SPIKE and row["ratio"] >= 0.55:
            db.insert_event(
                type="flow", source="FINRA", symbol=row["symbol"],
                title=f"{row['symbol']}: açığa satış hacmi oranı %{row['ratio']*100:.0f} (20g ort. %{avg*100:.0f})",
                published_at=f"{day}T21:00:00+00:00", dedupe_key=sha("finra-sv", row["symbol"], day),
                status="analyzed", importance=2, direction=-1,
                extra={"ratio": row["ratio"], "avg20": avg, "short_vol": row["short_vol"], "total_vol": row["total_vol"]},
            )


def short_volume_series(symbol: str, days: int = 60) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT day, short_vol, total_vol, ratio FROM short_volume WHERE symbol = ? ORDER BY day DESC LIMIT ?", (symbol, days))][::-1]


# ── FINRA short interest (settlement mid-month & month-end) ───────────────

SI_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
SI_ALERT_CHANGE = 15.0   # % change between reports


def collect_short_interest() -> int:
    n = 0
    first = db.get_cursor("finra_si_seen") is None
    for t in db.active_tickers():
        try:
            r = httpx.post(SI_URL, timeout=30, headers={"Accept": "application/json"}, json={
                "limit": 50,
                "compareFilters": [{"compareType": "equal", "fieldName": "symbolCode", "fieldValue": t["symbol"]}],
                "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": str(date.today() - timedelta(days=180)),
                                      "endDate": str(date.today())}],
            })
            r.raise_for_status()
            rows = r.json() if r.content else []
        except Exception as exc:  # noqa: BLE001
            log.warning("finra short interest %s failed: %s", t["symbol"], exc)
            continue
        for x in sorted(rows, key=lambda x: x["settlementDate"]):
            exists = db.query_one("SELECT 1 FROM short_interest WHERE symbol = ? AND settlement_date = ?", (t["symbol"], x["settlementDate"]))
            db.execute(
                """INSERT OR REPLACE INTO short_interest(symbol, settlement_date, short_qty, prev_qty, change_pct, days_to_cover, avg_daily_vol)
                   VALUES (?,?,?,?,?,?,?)""",
                (t["symbol"], x["settlementDate"], x.get("currentShortPositionQuantity"), x.get("previousShortPositionQuantity"),
                 x.get("changePercent"), x.get("daysToCoverQuantity"), x.get("averageDailyVolumeQuantity")),
            )
            if exists or first:
                continue
            n += 1
            chg = x.get("changePercent") or 0
            if abs(chg) >= SI_ALERT_CHANGE:
                db.insert_event(
                    type="flow", source="FINRA", symbol=t["symbol"],
                    title=(f"{t['symbol']}: açık pozisyon (short interest) {chg:+.1f}% → "
                           f"{(x.get('currentShortPositionQuantity') or 0)/1e6:,.1f}M hisse, kapatma süresi {x.get('daysToCoverQuantity')} gün"),
                    published_at=iso(utcnow()), dedupe_key=sha("finra-si", t["symbol"], x["settlementDate"]),
                    status="analyzed", importance=3 if abs(chg) < 30 else 4, direction=-1 if chg > 0 else 1,
                    extra={"settlement_date": x["settlementDate"], "short_qty": x.get("currentShortPositionQuantity"),
                           "change_pct": chg, "days_to_cover": x.get("daysToCoverQuantity")},
                )
    db.set_cursor("finra_si_seen", "1")
    return n


def short_interest_series(symbol: str, n: int = 12) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM short_interest WHERE symbol = ? ORDER BY settlement_date DESC LIMIT ?", (symbol, n))][::-1]


# ── Reddit mentions (ApeWisdom) ───────────────────────────────────────────

APE_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{p}"
APE_MULT = 3.0
APE_MIN = 15


def collect_social() -> int:
    syms = {t["symbol"] for t in db.active_tickers()}
    now = iso(utcnow())
    found = 0
    for p in range(1, 7):  # top ~600
        try:
            r = httpx.get(APE_URL.format(p=p), timeout=30)
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("apewisdom page %s failed: %s", p, exc)
            break
        for x in results:
            if x.get("ticker") not in syms:
                continue
            found += 1
            mentions, prev = int(x.get("mentions") or 0), int(x.get("mentions_24h_ago") or 0)
            db.execute(
                "INSERT OR REPLACE INTO social_mentions(symbol, ts, rank, mentions, mentions_24h, upvotes) VALUES (?,?,?,?,?,?)",
                (x["ticker"], now, x.get("rank"), mentions, prev, x.get("upvotes")),
            )
            if mentions >= APE_MIN and mentions >= APE_MULT * max(prev, 1):
                day = utcnow().strftime("%Y-%m-%d")
                db.insert_event(
                    type="flow", source="Reddit (ApeWisdom)", symbol=x["ticker"],
                    title=f"{x['ticker']}: Reddit'te anılma {prev} → {mentions} (24 saatte {mentions/max(prev,1):.1f}×), sıra #{x.get('rank')}",
                    published_at=now, dedupe_key=sha("ape", x["ticker"], day), status="analyzed", importance=3, direction=0,
                    extra={"mentions": mentions, "mentions_24h_ago": prev, "rank": x.get("rank"), "rank_24h_ago": x.get("rank_24h_ago")},
                )
        if len(results) < 100:
            break
    db.execute("DELETE FROM social_mentions WHERE ts < ?", (iso(utcnow() - timedelta(days=90)),))
    return found


def social_series(symbol: str, days: int = 30) -> list[dict]:
    """One point per day (last snapshot of each day)."""
    rows = db.query(
        """SELECT substr(ts,1,10) AS day, MAX(ts) AS ts FROM social_mentions WHERE symbol = ? AND ts >= ?
           GROUP BY substr(ts,1,10) ORDER BY day""",
        (symbol, iso(utcnow() - timedelta(days=days))),
    )
    out = []
    for r in rows:
        x = db.query_one("SELECT * FROM social_mentions WHERE symbol = ? AND ts = ?", (symbol, r["ts"]))
        out.append({"day": r["day"], "mentions": x["mentions"], "rank": x["rank"], "upvotes": x["upvotes"]})
    return out
