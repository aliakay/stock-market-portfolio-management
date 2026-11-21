"""SEC XBRL frames: one concept, one calendar period, every filer in one call (no quota; SEC fair-access pacing).

Used by the idea screener (`ideas.py`) to scan ~5,000 US companies instead of only the followed ones. Values are kept
in their own SQLite file (data/frames.db) so the production database does not grow.

Quarterly flows (revenue, net income, operating income, gross profit) come from CYyyyyQn frames. Fiscal fourth
quarters are not filed as quarters; they are derived as annual (CYyyyy frame) minus the three quarters inside it,
the way `collectors/xbrl.py` does per company. Balance-sheet items come from instant frames (CYyyyyQnI).

Caveat stated wherever results are shown: a frame holds the latest filed value for a period, so a later
restatement can replace the first-reported number.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, timedelta

import numpy as np

from . import config

log = logging.getLogger(__name__)
PATH = config.DATA_DIR / "frames.db"
FIRST_YEAR = 2018

FLOW = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "RevenuesNetOfInterestExpense"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "op_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
}
INSTANT = {
    "assets": ("us-gaap", ["Assets"], "USD"),
    "assets_cur": ("us-gaap", ["AssetsCurrent"], "USD"),
    "liab_cur": ("us-gaap", ["LiabilitiesCurrent"], "USD"),
    "liab": ("us-gaap", ["Liabilities"], "USD"),
    "cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], "USD"),
    "shares": ("dei", ["EntityCommonStockSharesOutstanding"], "shares"),
}


def _conn() -> sqlite3.Connection:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(PATH, timeout=30, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            item TEXT NOT NULL, concept TEXT NOT NULL, period TEXT NOT NULL, cik INTEGER NOT NULL,
            start TEXT, end TEXT, val REAL NOT NULL, PRIMARY KEY (item, concept, period, cik));
        CREATE TABLE IF NOT EXISTS fetched (url TEXT PRIMARY KEY, at REAL NOT NULL, n INTEGER);
        CREATE TABLE IF NOT EXISTS names (cik INTEGER PRIMARY KEY, ticker TEXT, name TEXT);
    """)
    return c


def periods(today: date | None = None) -> tuple[list[str], list[str], list[str]]:
    """(quarter frames, annual frames, instant frames) from FIRST_YEAR to the last finished quarter."""
    today = today or date.today()
    q_last = (today.year, (today.month - 1) // 3)          # quarters fully ended
    qs, ann, inst = [], [], []
    for y in range(FIRST_YEAR, today.year + 1):
        for q in range(1, 5):
            if (y, q) > (q_last[0], q_last[1]) or (y == q_last[0] and q > q_last[1]):
                continue
            qs.append(f"CY{y}Q{q}")
            inst.append(f"CY{y}Q{q}I")
        ann.append(f"CY{y}")        # the current year too: fiscal years that ended before today (e.g. September)
    return qs, ann, inst


def _fetch(c: sqlite3.Connection, item: str, tax: str, concept: str, unit: str, period: str, max_age_h: float) -> int:
    from .collectors.sec import _get
    url = f"https://data.sec.gov/api/xbrl/frames/{tax}/{concept}/{unit}/{period}.json"
    row = c.execute("SELECT at FROM fetched WHERE url = ?", (url,)).fetchone()
    if row and time.time() - row[0] < max_age_h * 3600:
        return -1
    try:
        data = _get(url).json().get("data") or []
    except Exception as exc:  # noqa: BLE001 - 404 = nobody filed this concept for this period
        if "404" not in str(exc):
            log.warning("frames %s failed: %s", url, exc)
        data = []
    rows = [(item, concept, period, int(d["cik"]), d.get("start"), d.get("end"), float(d["val"])) for d in data if d.get("val") is not None]
    c.execute("BEGIN")
    c.execute("DELETE FROM facts WHERE item = ? AND concept = ? AND period = ?", (item, concept, period))
    c.executemany("INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?)", rows)
    c.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)", (url, time.time(), len(rows)))
    c.execute("COMMIT")
    return len(rows)


def refresh(progress=None) -> dict:
    """Fetch every frame not fetched yet. Old periods are fetched once (refetched after 60 days for late filers and
    restatements); the last five quarters and the last two years are refetched after 20 hours."""
    from .collectors.sec import _get
    c = _conn()
    qs, ann, inst = periods()
    recent = set(qs[-5:] + inst[-5:] + ann[-2:])
    jobs = []
    for item, concepts in FLOW.items():
        for con in concepts:
            jobs += [(item, "us-gaap", con, "USD", p) for p in qs + ann]
    for item, (tax, concepts, unit) in INSTANT.items():
        for con in concepts:
            jobs += [(item, tax, con, unit, p) for p in inst]
    n_new = 0
    for k, (item, tax, con, unit, p) in enumerate(jobs):
        got = _fetch(c, item, tax, con, unit, p, 20 if p in recent else 24 * 60)
        n_new += got > 0
        if progress and k % 25 == 0:
            progress(k, len(jobs))
    try:
        data = _get("https://www.sec.gov/files/company_tickers.json").json()
        c.execute("BEGIN")
        c.execute("DELETE FROM names")
        seen = set()
        for v in data.values():          # first listed ticker per CIK (the main share class)
            cik = int(v["cik_str"])
            if cik in seen:
                continue
            seen.add(cik)
            c.execute("INSERT INTO names VALUES (?,?,?)", (cik, v["ticker"].upper().replace(".", "-"), v["title"]))
        c.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC ticker map failed: %s", exc)
    c.close()
    return {"jobs": len(jobs), "fetched": n_new}


# ── panel: companies × calendar quarters ──────────────────

class Panel:
    """Arrays [company, quarter]; NaN = not reported. `q` are period labels CYyyyyQn in order."""

    derived: np.ndarray | None = None

    def __init__(self, ciks: np.ndarray, q: list[str], data: dict[str, np.ndarray], names: dict[int, tuple[str, str]]):
        self.ciks, self.q, self.data, self.names = ciks, q, data, names
        self.row = {int(k): i for i, k in enumerate(ciks)}

    def __getitem__(self, item: str) -> np.ndarray:
        return self.data[item]


def _qidx(end: str) -> tuple[int, int]:
    """Calendar quarter a ~91-day period ending on `end` mostly falls in (as SEC frames assign them): the quarter of
    its midpoint. A fiscal quarter ending 26 January is the calendar fourth quarter; one ending 3 January too."""
    mid = date.fromisoformat(end) - timedelta(days=45)
    return mid.year, (mid.month - 1) // 3 + 1


def _concept_array(c, item: str, con: str, row: dict, qi: dict, nq: int) -> tuple[np.ndarray, np.ndarray]:
    """One concept as [company, quarter], with fiscal fourth quarters derived from the annual frame."""
    arr = np.full((len(row), nq), np.nan)
    der = np.zeros((len(row), nq), dtype=bool)
    ann: list[tuple[int, float, str]] = []
    for cik, period, end, val in c.execute("SELECT cik, period, end, val FROM facts WHERE item = ? AND concept = ?", (item, con)):
        i = row.get(cik)
        if i is None:
            continue
        if period in qi:
            arr[i, qi[period]] = val
        elif len(period) == 6 and end:
            ann.append((i, val, end))
    for i, val, end in ann:
        y, q = _qidx(end)
        j = qi.get(f"CY{y}Q{q}")
        if j is None or j < 3 or not np.isnan(arr[i, j]):
            continue
        prev = arr[i, j - 3:j]
        if not np.isnan(prev).any():
            arr[i, j] = val - prev.sum()
            der[i, j] = True
    return arr, der


def fill_gaps(item: str, arr: np.ndarray, der: np.ndarray, arrs: list[tuple[np.ndarray, np.ndarray]]) -> int:
    """Fill quarters the chosen concept lacks from the company's other concepts (in place). Companies switch tags over
    the years (Alphabet, Best Buy, AIG …), so one concept for 2018 → today leaves the latest quarters empty.
    Net income: the first other concept that has the quarter (NetIncomeLoss and ProfitLoss differ only by minority
    interests). Revenue: the largest other value within 0.5–2× of the nearest known quarter, so a sub-line (a bank's
    'Revenues' next to its total) is never mixed in. Returns the number of filled cells."""
    stack = np.stack([a for a, _ in arrs])          # [concept, company, quarter]
    dstack = np.stack([d for _, d in arrs])
    gaps = np.isnan(arr) & ~np.isnan(stack).all(axis=0)
    n = 0
    for i in np.flatnonzero(gaps.any(axis=1)):
        for j in np.flatnonzero(gaps[i]):
            cand = [(k, stack[k, i, j]) for k in range(len(arrs)) if not np.isnan(stack[k, i, j])]
            if item == "revenue":
                known = np.flatnonzero(~np.isnan(arr[i]))
                if len(known):
                    ref = abs(arr[i, known[np.argmin(np.abs(known - j))]])
                    cand = [(k, v) for k, v in cand if ref > 0 and 0.5 <= abs(v) / ref <= 2.0]
                if not cand:
                    continue
                k, v = max(cand, key=lambda kv: abs(kv[1]))
            else:
                k, v = cand[0]
            arr[i, j], der[i, j] = v, dstack[k, i, j]
            n += 1
    return n


def load_panel() -> Panel | None:
    """One concept per company and item, so growth rates never compare two different revenue lines: the concept
    with (near-)best coverage; among those, revenue takes the largest (a sub-line such as 'Revenues' of a bank is
    smaller than its total 'RevenuesNetOfInterestExpense'), net income the first in FLOW order."""
    if not PATH.exists():
        return None
    c = _conn()
    qs, ann, inst = periods()
    qi = {p: i for i, p in enumerate(qs)}
    names = {r[0]: (r[1], r[2]) for r in c.execute("SELECT cik, ticker, name FROM names")}
    ciks = np.array(sorted({r[0] for r in c.execute("SELECT DISTINCT cik FROM facts WHERE item = 'net_income'")}), dtype=np.int64)
    row = {int(k): i for i, k in enumerate(ciks)}
    nq = len(qs)
    data: dict[str, np.ndarray] = {}
    derived = np.zeros((len(ciks), nq), dtype=bool)
    for item, concepts in FLOW.items():
        arrs = [_concept_array(c, item, con, row, qi, nq) for con in concepts]
        cover = np.stack([(~np.isnan(a)).sum(axis=1) for a, _ in arrs])            # [concept, company]
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                size = np.stack([np.nan_to_num(np.nanmedian(np.abs(a), axis=1), nan=-1) for a, _ in arrs])
        ok = cover >= 0.8 * cover.max(axis=0, keepdims=True)
        ok &= cover > 0
        if item == "revenue":
            pick = np.argmax(np.where(ok, size, -2), axis=0)
        else:
            pick = np.argmax(ok, axis=0)                        # first concept in FLOW order with good coverage
        arr = np.full((len(ciks), nq), np.nan)
        der = np.zeros((len(ciks), nq), dtype=bool)
        for k, (a, d) in enumerate(arrs):
            sel = pick == k
            arr[sel], der[sel] = a[sel], d[sel]
        if len(arrs) > 1:
            fill_gaps(item, arr, der, arrs)
        data[item] = arr
        if item == "net_income":
            derived = der
    ii = {p: i for i, p in enumerate(inst)}
    for item, (_, concepts, _) in INSTANT.items():
        arr = np.full((len(ciks), nq), np.nan)
        for con in concepts:
            for cik, period, val in c.execute("SELECT cik, period, val FROM facts WHERE item = ? AND concept = ?", (item, con)):
                i = row.get(cik)
                j = ii.get(period)
                if i is None or j is None:
                    continue
                if np.isnan(arr[i, j]):
                    arr[i, j] = val
        data[item] = arr
    c.close()
    p = Panel(ciks, qs, data, names)
    p.derived = derived
    return p
