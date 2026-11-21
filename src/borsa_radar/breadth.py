"""S&P 500 market breadth from the shared daily bar store.

Constituents come from the Wikipedia list (cached 7 days in `api_cache`); their
bars are refreshed by a scheduler job after the close, never on page load.
Per trading day we store: % above 50/200-day averages, advancers/decliners,
new 52-week closing highs/lows, the cumulative A/D line and the McClellan
oscillator (EMA19 − EMA39 of net advances).
"""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta

import httpx
import pandas as pd

from . import config, db, prices, stockdata
from .util import utcnow_iso

log = logging.getLogger(__name__)
# Index member lists: the three pages share one table layout (id="constituents": Symbol, Security, GICS Sector).
INDEX_LISTS = {  # code: (page, label, members expected)
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P 500", 500),
    "sp400": ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "S&P 400", 400),
    "sp600": ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "S&P 600", 600),
}
WIKI_URL = INDEX_LISTS["sp500"][0]
SOURCE = "S&P 500 bileşenleri (Wikipedia listesi) · Yahoo günlük kapanış"
_WIKI_HEADERS = {"User-Agent": "index-list-reader/0.1 (personal research tool)"}


def index_members(code: str) -> stockdata.Fetched:
    """[{symbol, name, sector}] for one index list, cached 7 days (last good copy if the page cannot be read)."""
    url, label, expected = INDEX_LISTS[code]

    def fetch():
        r = httpx.get(url, headers=_WIKI_HEADERS, timeout=30, follow_redirects=True)
        r.raise_for_status()
        t = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
        out = [{"symbol": str(row["Symbol"]).strip().replace(".", "-"), "name": row["Security"], "sector": row["GICS Sector"]}
               for _, row in t.iterrows()]
        if len(out) < expected * 0.8:
            raise RuntimeError(f"{label}: beklenmeyen bileşen sayısı: {len(out)}")
        return out
    return stockdata._cached(f"wiki:{code}", f"Wikipedia {label} listesi", timedelta(days=7), fetch)


def constituents() -> stockdata.Fetched:
    return index_members("sp500")


def symbols() -> list[str]:
    f = constituents()
    return [c["symbol"] for c in f.data] if f else []


def refresh_prices() -> int:
    syms = symbols()
    if not syms:
        return 0
    out = prices.refresh(syms)
    return sum(1 for v in out.values() if v)


# ── math (pure, testable) ─────────────────────────────────

def compute_frame(closes: pd.DataFrame) -> pd.DataFrame:
    """closes: index = day (sorted), columns = symbols. Returns one row per day."""
    c = closes.sort_index()
    have = c.notna()
    sma50 = c.rolling(50, min_periods=50).mean()
    sma200 = c.rolling(200, min_periods=200).mean()
    hi = c.rolling(252, min_periods=200).max()
    lo = c.rolling(252, min_periods=200).min()
    prev = c.shift(1)
    v50 = sma50.notna() & have
    v200 = sma200.notna() & have
    out = pd.DataFrame(index=c.index)
    out["n"] = have.sum(axis=1)
    out["above50"] = ((c > sma50) & v50).sum(axis=1) / v50.sum(axis=1).replace(0, float("nan")) * 100
    out["above200"] = ((c > sma200) & v200).sum(axis=1) / v200.sum(axis=1).replace(0, float("nan")) * 100
    out["adv"] = ((c > prev) & prev.notna()).sum(axis=1)
    out["dec"] = ((c < prev) & prev.notna()).sum(axis=1)
    out["nh"] = ((c >= hi) & hi.notna()).sum(axis=1)
    out["nl"] = ((c <= lo) & lo.notna()).sum(axis=1)
    net = (out["adv"] - out["dec"]).astype(float)
    out["ad_line"] = net.cumsum()
    out["mcclellan"] = net.ewm(span=19, adjust=False).mean() - net.ewm(span=39, adjust=False).mean()
    return out


def _load_closes(syms: list[str], start: str) -> pd.DataFrame:
    rows = []
    for i in range(0, len(syms), 400):
        chunk = syms[i:i + 400]
        rows += db.query(f"SELECT symbol, day, close FROM prices_daily WHERE day >= ? AND close IS NOT NULL "
                         f"AND symbol IN ({','.join('?' * len(chunk))})", [start, *chunk])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([tuple(r) for r in rows], columns=["symbol", "day", "close"])
    piv = df.pivot_table(index="day", columns="symbol", values="close", aggfunc="last")
    cal = prices.trading_days(start)
    return piv.reindex(cal) if cal else piv


def rebuild(days_back: int = 3 * 366) -> int:
    """Recompute breadth for every stored day and upsert."""
    syms = symbols()
    if not syms:
        return 0
    start = (date.today() - timedelta(days=days_back)).isoformat()
    closes = _load_closes(syms, start)
    if closes.empty:
        return 0
    fr = compute_frame(closes)
    fr = fr[fr["above200"].notna()]
    now = utcnow_iso()
    rows = [(d, int(r.n), _f(r.above50), _f(r.above200), int(r.adv), int(r.dec), int(r.nh), int(r.nl), _f(r.ad_line),
             _f(r.mcclellan), SOURCE, now) for d, r in fr.iterrows() if r.n >= 300]
    with db.tx() as c:
        c.executemany("INSERT OR REPLACE INTO breadth_daily(day, n, above50, above200, adv, dec, nh, nl, ad_line, mcclellan, "
                      "source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _f(v):
    return None if v is None or v != v else round(float(v), 4)


def refresh() -> int:
    """Scheduler job: extend constituent bars, then recompute breadth."""
    n = refresh_prices()
    log.info("breadth: %s constituents updated", n)
    return rebuild()


# ── reads ─────────────────────────────────────────────────

def at(day: str) -> dict | None:
    r = db.query_one("SELECT * FROM breadth_daily WHERE day <= ? ORDER BY day DESC LIMIT 1", (day,))
    return dict(r) if r else None


def history(start: str | None = None) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM breadth_daily WHERE day >= ? ORDER BY day", (start or "0000",))]


def sector_breadth(day: str) -> dict:
    """Fallback / second opinion: share of the 11 sector ETFs above their 200-day (dividend-adjusted)."""
    above, have, rows = 0, 0, []
    for s, name in config.SECTOR_ETFS.items():
        bs = [b for b in prices.bars(s, (date.fromisoformat(day) - timedelta(days=420)).isoformat()) if b["day"] <= day]
        xs = [b["adj_close"] or b["close"] for b in bs if (b["adj_close"] or b["close"])]
        if len(xs) < 200:
            continue
        sma = sum(xs[-200:]) / 200
        have += 1
        ok = xs[-1] > sma
        above += ok
        rows.append({"symbol": s, "name": name, "above": ok, "dist": (xs[-1] / sma - 1) * 100})
    return {"pct": above / have * 100 if have else None, "n": have, "rows": rows}
