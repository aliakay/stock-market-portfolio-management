"""Long daily history for the strategy lab (research only; the live bar store is `prices_daily`).

The live store keeps ~3 years, which is almost all bull market. Strategy rules are judged on 2006 → today so
2008, 2018 Q4, 2020 and 2022 are inside the test. Bars are dividend- and split-adjusted (Yahoo auto_adjust), kept
in one compressed numpy file under data/ so the production database does not grow.

Survivorship bias: the default universe is point-in-time S&P 500 membership (free history from
github.com/fja05680/sp500, MIT) — a stock may only signal on days it was in the index, and former members that
still trade are downloaded into EX_PATH. Members that were delisted (taken over or bankrupt) have no free prices and
stay missing; `build_members` counts them so every result can state the size of that gap. The older universe
(today's S&P 500 + 400 lists) stays available for comparison.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from datetime import date

import httpx
import numpy as np
import yfinance as yf

from . import config

log = logging.getLogger(__name__)
HIST_PATH = config.DATA_DIR / "lab_history.npz"
START = "2005-01-01"
# market context symbols: index trend, volatility, credit, participation, rates, dollar
MACRO = ["SPY", "^VIX", "HYG", "LQD", "RSP", "IWM", "^TNX", "^IRX", "DX-Y.NYB"]
COLS = ("day", "open", "high", "low", "close", "volume")   # day as yyyymmdd int stored in a float column


def lab_universe() -> list[str]:
    from . import breadth, setups
    out: set[str] = set()
    for code in ("sp500", "sp400"):
        out.update(m["symbol"] for m in (breadth.index_members(code).data or []))
    out.update(u["symbol"] for u in setups.universe(wide=False))
    return sorted(out)


def _frame_to_arr(f) -> np.ndarray | None:
    if f is None or f.empty:
        return None
    f = f.dropna(subset=["Close"])
    if f.empty:
        return None
    days = np.array([int(ts.strftime("%Y%m%d")) for ts in f.index], dtype=np.float64)
    vol = f["Volume"].fillna(0).to_numpy(dtype=np.float64) if "Volume" in f else np.zeros(len(f))
    c = f["Close"].to_numpy(dtype=np.float64)
    o = f["Open"].fillna(f["Close"]).to_numpy(dtype=np.float64)
    h = f["High"].fillna(f["Close"]).to_numpy(dtype=np.float64)
    lo = f["Low"].fillna(f["Close"]).to_numpy(dtype=np.float64)
    return np.column_stack([days, o, h, lo, c, vol])


SMALL_PATH = config.DATA_DIR / "lab_history_sp600.npz"   # small caps, for the idea screener's backtest


def download(symbols: list[str] | None = None, start: str = START, chunk: int = 50, path=None) -> dict:
    """Fetch (or re-fetch) adjusted daily bars and write `path` (default HIST_PATH). Returns {symbols, missing, seconds}."""
    t0 = time.time()
    path = path or HIST_PATH
    syms = sorted(set((symbols or lab_universe()) + (MACRO if path == HIST_PATH else [])))
    out: dict[str, np.ndarray] = {}
    missing: list[str] = []
    for k in range(0, len(syms), chunk):
        part = syms[k:k + chunk]
        for attempt in range(3):
            try:
                df = yf.download(part, start=start, interval="1d", auto_adjust=True, group_by="ticker",
                                 progress=False, threads=True, multi_level_index=True)
                break
            except Exception as exc:  # noqa: BLE001 - Yahoo flakiness
                log.warning("lab download %s failed (%s), retry %d", part[:2], exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        else:
            missing.extend(part)
            continue
        for s in part:
            try:
                f = df[s] if s in set(df.columns.get_level_values(0)) else None
            except Exception:  # noqa: BLE001
                f = None
            a = _frame_to_arr(f)
            if a is None or len(a) < 260:
                missing.append(s)
            else:
                out[s] = a
        time.sleep(1)
    np.savez_compressed(path, **{s.replace("^", "_I_").replace("-", "_D_").replace(".", "_P_"): a for s, a in out.items()})
    return {"symbols": len(out), "missing": missing, "seconds": round(time.time() - t0)}


def _unkey(k: str) -> str:
    return k.replace("_I_", "^").replace("_D_", "-").replace("_P_", ".")


def load(path=None) -> dict[str, np.ndarray]:
    path = path or HIST_PATH
    if not path.exists():
        return {}
    with np.load(path) as z:
        return {_unkey(k): z[k] for k in z.files}


def download_small() -> dict:
    """S&P 600 members not already in the main history file."""
    from . import breadth
    have = {_unkey(k) for k in np.load(HIST_PATH).files} if HIST_PATH.exists() else set()
    syms = [m["symbol"] for m in (breadth.index_members("sp600").data or []) if m["symbol"] not in have]
    return download(syms, path=SMALL_PATH)


def as_of() -> str | None:
    """Last bar day in the file (for the 'data through' label)."""
    h = load()
    spy = h.get("SPY")
    if spy is None:
        return None
    d = int(spy[-1, 0])
    return date(d // 10000, d // 100 % 100, d % 100).isoformat()


# ── point-in-time S&P 500 membership (free sources) ───────

SP500_HIST_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
                  "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv")
SP500_HIST_PATH = config.DATA_DIR / "sp500_history.csv"     # date → members on that date, 1996 → (fja05680/sp500, MIT)
TICKERS_PATH = config.DATA_DIR / "sharadar_tickers.csv"      # Sharadar free `tickers` table, stock rows only
EX_PATH = config.DATA_DIR / "lab_history_ex.npz"             # bars of former members that still trade
MEMBERS_PATH = config.DATA_DIR / "sp500_members.json"        # resolved membership + the size of the remaining gap
MEMBERS_FROM = "2006-01-01"
OPEN_END = 99999999
_TICKER_COLS = ("permaticker", "ticker", "name", "exchange", "isdelisted", "category", "relatedtickers",
                "firstpricedate", "lastpricedate", "secfilings")


def _stale(path, days: int) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > days * 86400


def fetch_sources(max_age_days: int = 30) -> dict:
    """Download the membership history and Sharadar's free ticker table when missing or older than max_age_days."""
    got: dict = {}
    if _stale(SP500_HIST_PATH, max_age_days):
        r = httpx.get(SP500_HIST_URL, timeout=60, follow_redirects=True)
        if r.status_code != 200 or not r.text.startswith("date,tickers"):
            raise RuntimeError(f"S&P 500 history file: HTTP {r.status_code}")
        SP500_HIST_PATH.write_text(r.text)
        got["sp500_history"] = True
    key = config.secret("SHARADAR_API_KEY")
    if key and _stale(TICKERS_PATH, max_age_days):
        try:   # never let the key reach a log line: errors are re-raised without the request URL
            r = httpx.get("https://api.sharadar.com/v1.0/data/tickers", params={"api_key": key, "years": "full"},
                          timeout=120, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Sharadar tickers download failed: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise RuntimeError(f"Sharadar tickers: HTTP {r.status_code}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
        tmp = TICKERS_PATH.with_suffix(".tmp")
        with tmp.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_TICKER_COLS, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                if row.get("table") == "SEP":
                    w.writerow(row)
        tmp.replace(TICKERS_PATH)
        got["tickers"] = True
    return got


def read_history(path=None) -> list[tuple[str, list[str]]]:
    path = path or SP500_HIST_PATH
    with open(path, newline="") as f:
        return [(r["date"], [t.strip() for t in r["tickers"].split(",") if t.strip()]) for r in csv.DictReader(f)]


def membership_intervals(rows: list[tuple[str, list[str]]], since: str = MEMBERS_FROM) -> dict[str, list[list]]:
    """PURE. rows = (ISO date, members on that date) in date order → ticker → [[start, end], …]; `end` is the first
    day the ticker was no longer listed (exclusive) or None while still a member. Only intervals reaching `since`."""
    iv: dict[str, list[list]] = {}
    prev: set[str] = set()
    for d, members in rows:
        cur = set(members)
        for t in sorted(cur - prev):
            iv.setdefault(t, []).append([d, None])
        for t in prev - cur:
            iv[t][-1][1] = d
        prev = cur
    out = {}
    for t, v in iv.items():
        keep = [x for x in v if x[1] is None or x[1] > since]
        if keep:
            out[t] = keep
    return out


def _overlap(r: dict, s: str, e: str | None) -> int:
    a, b = max(r.get("firstpricedate") or "9999", s), min(r.get("lastpricedate") or "0000", e or "2999-12-31")
    return int((np.datetime64(b, "D") - np.datetime64(a, "D")).astype(int)) if a <= b else -1


def _day_before(d: str) -> str:
    return str(np.datetime64(d, "D") - np.timedelta64(1, "D"))


def _day_after(d: str) -> str:
    return str(np.datetime64(d, "D") + np.timedelta64(1, "D"))


def _pieces(t: str, a: str, b: str | None, by_t: dict, by_rel: dict) -> list[tuple[dict | None, str, str | None]]:
    """Split one membership interval into periods, each matched to one ticker-table record (or None).

    Cut points are where a candidate record's price history starts or ends. In each period: keep the previous
    period's record while it still covers (a company keeps its own history), else prefer the exact ticker over a
    related one (share classes), then the longest overlap."""
    cands = [(r, 1) for r in by_t.get(t, [])] + [(r, 0) for r in by_rel.get(t, [])]
    cands = [(r, ex) for r, ex in cands if _overlap(r, a, b) >= 0]
    if not cands:
        return [(None, a, b)]
    end = b or "2999-12-31"
    cuts = {a}
    for r, _ in cands:
        f, l_ = r.get("firstpricedate") or "", r.get("lastpricedate") or ""
        if a < f < end:
            cuts.add(f)
        if l_ and a < _day_after(l_) < end:
            cuts.add(_day_after(l_))
    cuts_s = sorted(cuts)
    out: list[list] = []
    prev = None
    for i, p in enumerate(cuts_s):
        q = cuts_s[i + 1] if i + 1 < len(cuts_s) else b

        def covers(r):
            if (r.get("firstpricedate") or "9999") > p:
                return False
            if q is None:
                return r.get("isdelisted") == "N"
            return (r.get("lastpricedate") or "") >= _day_before(q)
        cover = [(r, ex) for r, ex in cands if covers(r)]
        short = q is not None and (np.datetime64(q, "D") - np.datetime64(p, "D")).astype(int) <= 7
        if prev is not None and (any(r is prev for r, _ in cover) or (not cover and short)):
            pick = prev                     # same company, or a few days between a last price and an index removal
        elif cover:
            pick = max(cover, key=lambda c: (c[1], _overlap(c[0], p, q)))[0]
        else:
            best = max(cands, key=lambda c: (_overlap(c[0], p, q), c[1]))
            pick = best[0] if _overlap(best[0], p, q) >= 0 else None
        if out and out[-1][0] is pick:
            out[-1][2] = q
        else:
            out.append([pick, p, q])
        prev = pick
    return [tuple(x) for x in out]


def resolve(iv: dict[str, list[list]], ticker_rows: list[dict], have: set[str], since: str = MEMBERS_FROM) -> dict[str, list[dict]]:
    """PURE. Historical index ticker → one entry per matched period: {"sym": Yahoo symbol or None, "status", "name",
    "last", "iv": [start, end]}.

    status: in_hist (bars on file) · listed (still trades, bars not downloaded yet) · delisted (no free prices) ·
    unmatched (not in the ticker table). Periods are matched to records by price-history dates (`_pieces`), so a
    recycled ticker (CEG, DOW: an older and a newer company) or a chain (21st Century Fox → Fox Corp under FOXA)
    maps each period to the right company."""
    by_t: dict[str, list[dict]] = {}
    by_rel: dict[str, list[dict]] = {}
    for r in ticker_rows:
        by_t.setdefault(r["ticker"], []).append(r)
        for x in (r.get("relatedtickers") or "").split():
            by_rel.setdefault(x, []).append(r)
    out: dict[str, list[dict]] = {}
    for t, v in iv.items():
        for a, b in v:
            a = max(a, since)
            if not ticker_rows:   # no ticker table: best effort by name
                sym = t.replace(".", "-")
                out.setdefault(t, []).append({"sym": sym, "status": "in_hist" if sym in have else "unmatched", "name": "",
                                              "last": None, "iv": [a, b]})
                continue
            for r, p, q in _pieces(t, a, b, by_t, by_rel):
                if r is None:
                    ent = {"sym": None, "status": "unmatched", "name": "", "last": None}
                elif r.get("isdelisted") == "Y":
                    ent = {"sym": None, "status": "delisted", "name": r.get("name", ""), "last": r.get("lastpricedate")}
                else:
                    sym = r["ticker"].replace(".", "-")
                    ent = {"sym": sym, "status": "in_hist" if sym in have else "listed", "name": r.get("name", ""), "last": None}
                out.setdefault(t, []).append(ent | {"iv": [p, q]})
    return out


def _merge(iv: list[list]) -> list[list]:
    out: list[list] = []
    for s, e in sorted(iv, key=lambda x: x[0]):
        if out and (out[-1][1] is None or s <= out[-1][1]):
            if out[-1][1] is not None and (e is None or e > out[-1][1]):
                out[-1][1] = e
        else:
            out.append([s, e])
    return out


def _bdays(iv: list[list], since: str, today: str) -> int:
    n = 0
    for s, e in iv:
        a, b = max(s, since), min(e or today, today)
        if a < b:
            n += int(np.busday_count(np.datetime64(a), np.datetime64(b)))
    return n


def _have() -> set[str]:
    out: set[str] = set()
    for p in (HIST_PATH, EX_PATH):
        if p.exists():
            with np.load(p) as z:
                out.update(_unkey(k) for k in z.files)
    return out


def build_members(refresh: bool = True, current: list[str] | None = None) -> dict:
    """Resolve point-in-time S&P 500 membership to Yahoo symbols and measure what is still missing. Writes MEMBERS_PATH."""
    if refresh:
        fetch_sources()
    rows = read_history()
    csv_through = rows[-1][0]
    today = date.today().isoformat()
    iv = membership_intervals(rows)
    trows: list[dict] = []
    if TICKERS_PATH.exists():
        with TICKERS_PATH.open(newline="") as f:
            trows = list(csv.DictReader(f))
    have = _have()
    res = resolve(iv, trows, have)
    by_sym: dict[str, list[list]] = {}
    for t, ents in res.items():
        for r in ents:
            if r["sym"] and r["status"] in ("in_hist", "listed"):
                by_sym.setdefault(r["sym"], []).append(list(r["iv"]))
    by_sym = {s: _merge(v) for s, v in by_sym.items()}
    if current is None:   # changes after the history file's last date: today's list (exact dates unknown)
        from . import breadth
        current = [m["symbol"] for m in (breadth.index_members("sp500").data or [])]
    if current:
        cur = set(current)
        open_now = {s for s, v in by_sym.items() if v[-1][1] is None}
        for s in open_now - cur:
            by_sym[s][-1][1] = today
        for s in cur - open_now:
            by_sym.setdefault(s, []).append([csv_through, None])
    by_status: dict[str, dict] = {}
    seen: set = set()
    for t, ents in res.items():
        for r in ents:
            r["days"] = _bdays([r["iv"]], MEMBERS_FROM, today)
            b = by_status.setdefault(r["status"], {"names": 0, "member_days": 0})
            who = (t, r["sym"] or r["name"])
            if r["days"] >= 5 and (who, r["status"]) not in seen:   # slivers between two records are not names
                seen.add((who, r["status"]))
                b["names"] += 1
            b["member_days"] += r["days"]
    tot = sum(b["member_days"] for b in by_status.values()) or 1
    for b in by_status.values():
        b["share_days"] = round(b["member_days"] / tot * 100, 1)
    gone = sorted(({"ticker": t, "name": r["name"], "last": r["last"], "days": r["days"],
                    "q": bool(r["name"]) and (r["name"].endswith(" Q") or t.endswith("Q"))}
                   for t, ents in res.items() for r in ents if r["status"] in ("delisted", "unmatched") and r["days"] >= 5),
                  key=lambda x: x["last"] or "")
    out = {"built": today, "csv_through": csv_through, "since": MEMBERS_FROM, "tickers_table": bool(trows),
           "names": len(iv), "status": by_status, "missing": gone,
           "members": {s: [[int(a.replace("-", "")), int(b.replace("-", "")) if b else OPEN_END] for a, b in v]
                       for s, v in sorted(by_sym.items())}}
    MEMBERS_PATH.write_text(json.dumps(out, ensure_ascii=False))
    return out


def download_ex() -> dict:
    """Bars for members that are not in the main history file (mostly former members that still trade)."""
    m = json.loads(MEMBERS_PATH.read_text())
    main: set[str] = set()
    if HIST_PATH.exists():
        with np.load(HIST_PATH) as z:
            main = {_unkey(k) for k in z.files}
    syms = sorted(s for s in m["members"] if s not in main)
    return download(syms, path=EX_PATH) if syms else {"symbols": 0, "missing": [], "seconds": 0}


def refresh_members() -> dict:
    """Monthly: sources → membership → bars for former members → membership again (statuses now see those bars)."""
    build_members(refresh=True)
    dl = download_ex()
    m = build_members(refresh=False)
    m["download"] = {"symbols": dl["symbols"], "missing": len(dl["missing"])}
    return m


def members_map() -> dict[str, np.ndarray]:
    """{Yahoo symbol: int array [[start, end_exclusive], …] (yyyymmdd)} or {} when never built."""
    if not MEMBERS_PATH.exists():
        return {}
    m = json.loads(MEMBERS_PATH.read_text())
    return {s: np.array(v, dtype=np.int64) for s, v in m["members"].items()}


def members_info() -> dict | None:
    """The gap summary of the last build (without the per-symbol intervals)."""
    if not MEMBERS_PATH.exists():
        return None
    m = json.loads(MEMBERS_PATH.read_text())
    m.pop("members", None)
    return m


class Hist:
    """The main history file plus former members (EX_PATH), read lazily like np.load."""

    def __init__(self, paths=None):
        self._z = [np.load(p) for p in (paths or (HIST_PATH, EX_PATH)) if p.exists()]
        self._where: dict = {}
        for z in self._z:
            for k in z.files:
                self._where.setdefault(k, z)
        self.files = list(self._where)

    def __getitem__(self, k):
        return self._where[k][k]


def universe(mode: str = "pit"):
    """(mode used, history reader, members or None). "pit" = point-in-time S&P 500 incl. former members that still
    trade; "today" = today's S&P 500 + 400 + own list (the older, survivor-only universe). Falls back to "today" when
    the membership file was never built."""
    if mode == "pit":
        mem = members_map()
        if mem:
            return "pit", Hist(), mem
    return "today", np.load(HIST_PATH), None


def delisting_by_cik() -> dict[int, int | None]:
    """{SEC CIK: last price day (yyyymmdd) when every share class was delisted, None while one still trades}, from the
    free Sharadar ticker table; {} when that table was never downloaded."""
    if not TICKERS_PATH.exists():
        return {}
    import re
    agg: dict[int, list] = {}
    with TICKERS_PATH.open(newline="") as f:
        for r in csv.DictReader(f):
            m = re.search(r"CIK=0*(\d+)", r.get("secfilings") or "")
            if not m:
                continue
            a = agg.setdefault(int(m.group(1)), [True, ""])
            a[0] = a[0] and r.get("isdelisted") == "Y"
            a[1] = max(a[1], r.get("lastpricedate") or "")
    return {cik: (int(last.replace("-", "")) if dead and last else None) for cik, (dead, last) in agg.items()}
