"""US macro: news feeds, economic calendar (FRED + Fed FOMC page) and data releases (FRED)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import feedparser
import httpx

from .. import config, db
from ..util import iso, jaccard, normalize_url, parse_dt, sha, strip_html, title_key, title_tokens, utcnow

log = logging.getLogger(__name__)

FEEDS = [
    ("Fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Fed (konuşma)", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("CNBC Ekonomi", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Piyasalar", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss"),
]
FRESH_HOURS = 24
NEAR_DUP = 0.65


# ── news ──────────────────────────────────────────────────

def collect_news() -> int:
    first_run = db.get_setting("macro_backfilled") != "1"
    recent = [
        (r["id"], title_tokens(r["title"]))
        for r in db.query("SELECT id, title FROM events WHERE type = 'macro' AND published_at >= ?",
                          (iso(utcnow() - timedelta(hours=72)),))
    ]
    fresh_cut = utcnow() - timedelta(hours=FRESH_HOURS)
    new = 0
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url, agent=config.BROWSER_UA)
        except Exception as exc:  # noqa: BLE001
            log.warning("macro feed %s failed: %s", source, exc)
            continue
        for e in feed.entries[:40]:
            title = (e.get("title") or "").strip()
            pub = parse_dt(e.get("published_parsed") or e.get("published") or e.get("updated"))
            if not title or not pub:
                continue
            toks = title_tokens(title)
            if any(jaccard(toks, t) >= NEAR_DUP for _, t in recent):
                continue
            link = normalize_url(e.get("link"))
            status = "backfill" if (first_run or pub < fresh_cut) else "pending"
            eid = db.insert_event(
                type="macro", source=source, url=e.get("link"), title=title,
                body=strip_html(e.get("summary"), 1500) or None, published_at=iso(pub),
                dedupe_key=sha("macro", link or title_key(title)), title_key=title_key(title), status=status,
            )
            if eid:
                recent.append((eid, toks))
                new += status == "pending"
    if first_run:
        db.set_setting("macro_backfilled", "1")
    return new


# ── calendar ──────────────────────────────────────────────

@dataclass(frozen=True)
class Release:
    key: str
    name: str
    fred_release_id: int | None
    et_time: time
    importance: int


RELEASES = [
    Release("cpi", "TÜFE / Enflasyon (CPI)", 10, time(8, 30), 5),
    Release("nfp", "Tarım dışı istihdam (NFP) & işsizlik", 50, time(8, 30), 5),
    Release("pce", "PCE fiyat endeksi (Fed'in enflasyon ölçüsü)", 54, time(8, 30), 4),
    Release("gdp", "GSYİH (GDP)", 53, time(8, 30), 4),
    Release("ppi", "ÜFE (PPI)", 46, time(8, 30), 3),
    Release("retail", "Perakende satışlar", 9, time(8, 30), 3),
    Release("jolts", "JOLTS açık iş ilanları", 192, time(10, 0), 3),
    Release("indpro", "Sanayi üretimi", 13, time(9, 15), 2),
    Release("claims", "Haftalık işsizlik başvuruları", 180, time(8, 30), 2),
]
FOMC = Release("fomc", "FOMC faiz kararı", None, time(14, 0), 5)
BY_KEY = {r.key: r for r in RELEASES + [FOMC]}


def _et(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=config.NY_TZ).astimezone(timezone.utc)


def _upsert_calendar(r: Release, when: datetime) -> None:
    db.execute(
        "INSERT OR IGNORE INTO macro_calendar(key, name, release_at, importance) VALUES (?, ?, ?, ?)",
        (r.key, r.name, iso(when), r.importance),
    )


def _fred(path: str, **params):
    key = config.secret("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY missing")
    r = httpx.get(f"https://api.stlouisfed.org/fred/{path}", params={"api_key": key, "file_type": "json", **params}, timeout=30)
    if r.status_code >= 400:  # keep the URL (it carries the API key) out of logs
        raise RuntimeError(f"FRED {path} HTTP {r.status_code}")
    return r.json()


def refresh_earnings(days: int = 90) -> int:
    """Watchlist earnings dates (Finnhub)."""
    key = config.secret("FINNHUB_API_KEY")
    if not key:
        return 0
    today = date.today()
    n = 0
    for t in db.active_tickers():
        try:
            r = httpx.get("https://finnhub.io/api/v1/calendar/earnings", timeout=20, params={
                "from": str(today - timedelta(days=2)), "to": str(today + timedelta(days=days)),
                "symbol": t["symbol"], "token": key})
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("earnings %s failed: %s", t["symbol"], exc)
            continue
        for x in r.json().get("earningsCalendar") or []:
            hour = x.get("hour")
            et = {"bmo": time(7, 0), "amc": time(16, 5)}.get(hour, time(12, 0))
            when = _et(date.fromisoformat(x["date"]), et)
            label = {"bmo": "açılış öncesi", "amc": "kapanış sonrası"}.get(hour, "saat belirsiz")
            eps = f", EPS beklentisi {x['epsEstimate']:.2f}" if x.get("epsEstimate") is not None else ""
            name = f"{t['symbol']} bilanço ({label}{eps})"
            key_ = f"earnings:{t['symbol']}"
            # date may move: drop stale future entries for this symbol first
            db.execute("DELETE FROM macro_calendar WHERE key = ? AND released = 0 AND release_at > ? AND release_at != ?",
                       (key_, iso(utcnow()), iso(when)))
            db.execute(
                "INSERT INTO macro_calendar(key, name, release_at, importance) VALUES (?, ?, ?, 4) "
                "ON CONFLICT(key, release_at) DO UPDATE SET name = excluded.name",
                (key_, name, iso(when)),
            )
            n += 1
    return n


def refresh_calendar(days: int = 45) -> int:
    n = refresh_earnings()
    today = date.today()
    try:
        data = _fred("releases/dates", realtime_start=str(today - timedelta(days=3)),
                     realtime_end=str(today + timedelta(days=days)),
                     include_release_dates_with_no_data="true", limit=1000, sort_order="asc")
        ids = {r.fred_release_id: r for r in RELEASES}
        for x in data.get("release_dates", []):
            r = ids.get(x["release_id"])
            if r:
                _upsert_calendar(r, _et(date.fromisoformat(x["date"]), r.et_time))
                n += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("FRED calendar failed: %s", exc)

    try:
        html = httpx.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                         headers={"User-Agent": config.BROWSER_UA}, timeout=30, follow_redirects=True).text
        months = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                              "August", "September", "October", "November", "December"], 1)}
        for year in (today.year, today.year + 1):
            start = html.find(f"{year} FOMC Meetings")
            if start == -1:
                continue
            end = html.find("FOMC Meetings", start + 20)
            seg = html[start: end if end != -1 else start + 20000]
            for m in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', seg, re.S):
                month_txt, days_txt = m.group(1).strip(), m.group(2).strip()
                month_name = month_txt.split("/")[-1].strip()
                last_day = re.findall(r"\d+", days_txt)
                if month_name not in months or not last_day:
                    continue
                # "Apr/May 30-1": decision day is the last day, in the last month.
                d = date(year, months[month_name], int(last_day[-1]))
                if today - timedelta(days=3) <= d <= today + timedelta(days=days + 60):
                    _upsert_calendar(FOMC, _et(d, FOMC.et_time))
                    n += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("FOMC calendar failed: %s", exc)
    return n


# ── data releases (actual values from FRED) ───────────────

def _obs(series: str, n: int = 14) -> list[tuple[str, float]]:
    data = _fred("series/observations", series_id=series, sort_order="desc", limit=n)
    out = []
    for o in data.get("observations", []):
        try:
            out.append((o["date"], float(o["value"])))
        except ValueError:
            continue
    return out  # newest first


def _updated_since(series: str, since: datetime) -> bool:
    info = _fred("series", series_id=series)["seriess"][0]
    lu = parse_dt(info.get("last_updated", "").replace(" ", "T", 1))
    return bool(lu and lu >= since - timedelta(minutes=5))


def _yoy(o):
    return (o[0][1] / o[12][1] - 1) * 100, (o[1][1] / o[13][1] - 1) * 100


def _mom(o):
    return (o[0][1] / o[1][1] - 1) * 100, (o[1][1] / o[2][1] - 1) * 100


def _fmt(x: float, d: int = 1) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _month(datestr: str) -> str:
    aylar = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
    d = date.fromisoformat(datestr)
    return f"{aylar[d.month - 1]} {d.year}"


def _values(key: str) -> tuple[str, dict, str] | None:
    """Return (headline, values dict, primary series) for a release, from FRED."""
    if key == "cpi":
        h, c = _obs("CPIAUCSL"), _obs("CPILFESL")
        (y, yp), (m, mp), (cy, cyp) = _yoy(h), _mom(h), _yoy(c)
        return (f"CPI ({_month(h[0][0])}): yıllık %{_fmt(y)} (önceki %{_fmt(yp)}), aylık %{_fmt(m)}; çekirdek yıllık %{_fmt(cy)} (önceki %{_fmt(cyp)})",
                {"yoy": y, "yoy_prev": yp, "mom": m, "mom_prev": mp, "core_yoy": cy, "core_yoy_prev": cyp}, "CPIAUCSL")
    if key == "pce":
        h, c = _obs("PCEPI"), _obs("PCEPILFE")
        (y, yp), (cy, cyp) = _yoy(h), _yoy(c)
        return (f"PCE ({_month(h[0][0])}): yıllık %{_fmt(y)} (önceki %{_fmt(yp)}); çekirdek %{_fmt(cy)} (önceki %{_fmt(cyp)})",
                {"yoy": y, "yoy_prev": yp, "core_yoy": cy, "core_yoy_prev": cyp}, "PCEPI")
    if key == "ppi":
        h = _obs("PPIFIS")
        (y, yp), (m, mp) = _yoy(h), _mom(h)
        return (f"PPI ({_month(h[0][0])}): yıllık %{_fmt(y)} (önceki %{_fmt(yp)}), aylık %{_fmt(m)}",
                {"yoy": y, "yoy_prev": yp, "mom": m, "mom_prev": mp}, "PPIFIS")
    if key == "nfp":
        p, u = _obs("PAYEMS", 3), _obs("UNRATE", 2)
        chg, chg_prev = p[0][1] - p[1][1], p[1][1] - p[2][1]
        return (f"İstihdam ({_month(p[0][0])}): NFP {chg:+,.0f}K (önceki {chg_prev:+,.0f}K), işsizlik %{_fmt(u[0][1])} (önceki %{_fmt(u[1][1])})",
                {"nfp_k": chg, "nfp_prev_k": chg_prev, "unrate": u[0][1], "unrate_prev": u[1][1]}, "PAYEMS")
    if key == "gdp":
        g = _obs("A191RL1Q225SBEA", 2)
        return (f"GSYİH ({g[0][0][:7]} çeyrek): yıllıklandırılmış %{_fmt(g[0][1])} (önceki %{_fmt(g[1][1])})",
                {"gdp": g[0][1], "gdp_prev": g[1][1]}, "A191RL1Q225SBEA")
    if key == "retail":
        r = _obs("RSAFS", 3)
        m, mp = _mom(r)
        return (f"Perakende satışlar ({_month(r[0][0])}): aylık %{_fmt(m)} (önceki %{_fmt(mp)})", {"mom": m, "mom_prev": mp}, "RSAFS")
    if key == "jolts":
        j = _obs("JTSJOL", 2)
        return (f"JOLTS ({_month(j[0][0])}): {j[0][1]/1000:.2f}M açık pozisyon (önceki {j[1][1]/1000:.2f}M)",
                {"openings_k": j[0][1], "prev_k": j[1][1]}, "JTSJOL")
    if key == "indpro":
        r = _obs("INDPRO", 3)
        m, mp = _mom(r)
        return (f"Sanayi üretimi ({_month(r[0][0])}): aylık %{_fmt(m)} (önceki %{_fmt(mp)})", {"mom": m, "mom_prev": mp}, "INDPRO")
    if key == "claims":
        c = _obs("ICSA", 2)
        return (f"Haftalık işsizlik başvuruları: {c[0][1]/1000:.0f}K (önceki {c[1][1]/1000:.0f}K)",
                {"claims_k": c[0][1] / 1000, "prev_k": c[1][1] / 1000}, "ICSA")
    return None


def check_releases() -> int:
    """For calendar entries whose time has passed, publish actual values once FRED has them."""
    now = utcnow()
    rows = db.query(
        "SELECT * FROM macro_calendar WHERE released = 0 AND release_at <= ? AND release_at >= ?",
        (iso(now), iso(now - timedelta(days=3))),
    )
    new = 0
    for row in rows:
        rel_at = parse_dt(row["release_at"])
        if row["key"] == "fomc" or row["key"].startswith("earnings:"):
            # FOMC arrives via the Fed press feed, earnings via SEC 8-K + news.
            if now - rel_at > timedelta(minutes=30):
                db.execute("UPDATE macro_calendar SET released = 1 WHERE id = ?", (row["id"],))
            continue
        try:
            got = _values(row["key"])
            if not got:
                db.execute("UPDATE macro_calendar SET released = 1 WHERE id = ?", (row["id"],))
                continue
            headline, values, series = got
            if not _updated_since(series, rel_at):
                continue
        except Exception as exc:  # noqa: BLE001
            log.warning("release %s check failed: %s", row["key"], exc)
            continue
        eid = db.insert_event(
            type="data", source="FRED", title=f"📊 {headline}",
            body=f"{row['name']} açıklandı. Değerler: {values}",
            published_at=row["release_at"], dedupe_key=sha("data", row["key"], row["release_at"]),
            status="pending", importance=row["importance"], extra={"key": row["key"], **values},
        )
        db.execute("UPDATE macro_calendar SET released = 1, event_id = ? WHERE id = ?", (eid, row["id"]))
        new += 1
    return new


def upcoming(hours: int = 48) -> list:
    now = utcnow()
    return db.query(
        "SELECT * FROM macro_calendar WHERE release_at >= ? AND release_at <= ? ORDER BY release_at",
        (iso(now - timedelta(hours=2)), iso(now + timedelta(hours=hours))),
    )
