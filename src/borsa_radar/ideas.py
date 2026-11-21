"""Fikir listesi: rule-based stock ideas from the SEC filings of ~5,000 US companies.

Tier 1 (every filer, SEC frames, no quota) applies four published screening rules to quarterly numbers:

* kara_yakin  — "Kâra yaklaşan": losses in ≥3 of the last 4 quarters, the loss narrowing two quarters in a row and
                smaller than a year ago, revenue up ≥15 % on the year, little dilution (Zacks: losses getting smaller
                each sequential quarter; GeoInvesting: losses + improving margins).
* ilk_kar     — "İlk kâr": first profitable quarter after ≥3 losing ones, revenue growing (Zacks "First Profit").
* hizlanan    — "Hızlanan büyüme": profitable, net income ≥ +25 % on the year, revenue ≥ +15 % and growing faster than
                the quarter before (CANSLIM C and A).
* iyilesen    — "Finansalı iyileşen": ≥6 of 7 Piotroski-style tests on trailing-twelve-month numbers (profit, rising
                return on assets, lower leverage, better current ratio, no dilution, better gross margin and asset
                turnover). The cash-flow tests are left out: quarterly cash flow is not in the frames.

Tier 2 (only the shortlist, Yahoo): price trend and liquidity, analysts' estimates and revisions, rating changes,
insider purchases, latest headlines. These are shown as facts next to each idea; they do not change the rule result.

The page shows, per rule, how its picks did in the past (`backtest`) and how this list's own picks do from the day
they were listed (live record). Nothing here is a buy recommendation.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from . import db, frames
from .util import utcnow_iso

log = logging.getLogger(__name__)
CURSOR_BT = "ideas_backtest"
MIN_REV_TTM = 50e6           # revenue over the last four quarters (shells and pre-revenue biotech out)
LAG_DAYS = 50                # a quarter's 10-Q is public by then (large filers 40 days, small 45)
LAG_DAYS_FY = 95             # a derived fiscal Q4 needs the 10-K (60-90 days)

SCREENS = {
    "kara_yakin": ("Kâra yaklaşan", "Zarar ediyor ama zarar art arda iki çeyrektir küçülüyor ve bir yıl öncesinden az; gelir yılda en az %15 büyüyor."),
    "ilk_kar": ("İlk kâr", "En az üç zararlı çeyreğin ardından ilk kez kâr açıkladı; gelir büyüyor."),
    "hizlanan": ("Hızlanan büyüme", "Kârlı; net kâr bir yılda en az %25 arttı, gelir en az %15 büyüyor ve büyüme bir önceki çeyrekten hızlı."),
    "iyilesen": ("Finansalı iyileşen", "Piotroski benzeri 7 testin en az 6'sını geçiyor: kârlılık, borçluluk, likidite, hisse ihracı, marj ve verimlilik bir yıl öncesinden iyi."),
}


# ── tier 1: rules on the quarterly panel ──────────────────

def _lag(arr: np.ndarray, aj: np.ndarray, k: int) -> np.ndarray:
    """arr[i, aj[i] - k] (NaN where out of range or aj < 0)."""
    j = aj - k
    ok = (j >= 0) & (aj >= 0)
    out = np.full(len(aj), np.nan)
    out[ok] = arr[np.flatnonzero(ok), j[ok]]
    return out


def _ttm(arr: np.ndarray, aj: np.ndarray, k0: int = 0) -> np.ndarray:
    return sum(_lag(arr, aj, k0 + k) for k in range(4))


def metrics(p: frames.Panel, aj: np.ndarray) -> dict[str, np.ndarray]:
    """Per-company numbers at each company's as-of quarter `aj` (index into p.q; −1 = none)."""
    ni = [_lag(p["net_income"], aj, k) for k in range(9)]
    rev = [_lag(p["revenue"], aj, k) for k in range(9)]
    gp = [_lag(p["gross_profit"], aj, k) for k in range(8)]
    with np.errstate(invalid="ignore", divide="ignore"):
        rev_yoy = np.where(rev[4] > 0, rev[0] / rev[4] - 1, np.nan)
        rev_yoy_prev = np.where(rev[5] > 0, rev[1] / rev[5] - 1, np.nan)
        ni_yoy = np.where(ni[4] > 0, ni[0] / ni[4] - 1, np.nan)
        rev_ttm, rev_ttm_prev = _ttm(p["revenue"], aj), _ttm(p["revenue"], aj, 4)
        ni_ttm, ni_ttm_prev = _ttm(p["net_income"], aj), _ttm(p["net_income"], aj, 4)
        gp_ttm, gp_ttm_prev = _ttm(p["gross_profit"], aj), _ttm(p["gross_profit"], aj, 4)
        assets, assets_prev = _lag(p["assets"], aj, 0), _lag(p["assets"], aj, 4)
        liab, liab_prev = _lag(p["liab"], aj, 0), _lag(p["liab"], aj, 4)
        ca, ca_prev = _lag(p["assets_cur"], aj, 0), _lag(p["assets_cur"], aj, 4)
        cl, cl_prev = _lag(p["liab_cur"], aj, 0), _lag(p["liab_cur"], aj, 4)
        sh, sh_prev = _lag(p["shares"], aj, 0), _lag(p["shares"], aj, 4)
        roa, roa_prev = ni_ttm / assets, ni_ttm_prev / assets_prev
        tests = [
            ni_ttm > 0,
            roa > roa_prev,
            liab / assets < liab_prev / assets_prev,
            ca / cl > ca_prev / cl_prev,
            sh <= sh_prev * 1.02,
            gp_ttm / rev_ttm > gp_ttm_prev / rev_ttm_prev,
            rev_ttm / assets > rev_ttm_prev / assets_prev,
        ]
        known = [~np.isnan(x) for x in (ni_ttm, roa_prev, liab_prev, cl_prev, sh_prev, gp_ttm_prev, rev_ttm_prev)]
        fscore = sum(t.astype(int) for t in tests)
        f_known = sum(k.astype(int) for k in known)
        losses4 = sum((ni[k] < 0).astype(int) for k in range(4))
        dil = np.where(sh_prev > 0, sh / sh_prev - 1, np.nan)
        # straight-line quarters to break-even from the last four quarters' trend
        slope = (ni[0] - ni[3]) / 3
        q_be = np.where((ni[0] < 0) & (slope > 0), -ni[0] / slope, np.nan)
        gm, gm_prev = gp[0] / rev[0], gp[4] / rev[4]
    return {"ni": ni, "rev": rev, "rev_yoy": rev_yoy, "rev_yoy_prev": rev_yoy_prev, "ni_yoy": ni_yoy, "rev_ttm": rev_ttm,
            "ni_ttm": ni_ttm, "fscore": fscore, "f_known": f_known, "losses4": losses4, "dil": dil, "q_be": q_be,
            "gm": gm, "gm_prev": gm_prev, "cash": _lag(p["cash"], aj, 0), "shares": sh}


def rules(m: dict) -> dict[str, np.ndarray]:
    """PURE: boolean mask per screen."""
    ni, rev = m["ni"], m["rev"]
    with np.errstate(invalid="ignore"):
        big = m["rev_ttm"] >= MIN_REV_TTM
        kara_yakin = (big & (ni[0] < 0) & (m["losses4"] >= 3) & (ni[0] > ni[1]) & (ni[1] > ni[2]) & (ni[0] > ni[4])
                      & (m["rev_yoy"] >= 0.15) & ~(m["dil"] > 0.15))
        ilk_kar = big & (ni[0] > 0) & (ni[1] < 0) & (ni[2] < 0) & (ni[3] < 0) & (m["rev_yoy"] >= 0.10)
        hizlanan = (big & (ni[0] > 0) & (ni[4] > 0) & (m["ni_yoy"] >= 0.25) & (m["rev_yoy"] >= 0.15)
                    & (m["rev_yoy"] > m["rev_yoy_prev"]) & (m["ni_ttm"] > 0))
        iyilesen = big & (m["f_known"] == 7) & (m["fscore"] >= 6)
    return {"kara_yakin": kara_yakin, "ilk_kar": ilk_kar, "hizlanan": hizlanan, "iyilesen": iyilesen}


def _q_end(label: str) -> date:
    y, q = int(label[2:6]), int(label[7])
    return date(y, q * 3, 30 if q in (2, 3) else 31)


def as_of_index(p: frames.Panel, when: date, derived: np.ndarray | None = None, live: bool = False) -> np.ndarray:
    """Per company: the last quarter whose filing would have been public on `when` (−1 = none in the last two).
    live=True: what is in the frames today is public by definition (no publication lag)."""
    ends = [_q_end(q) for q in p.q]
    have = ~np.isnan(p["net_income"]) & ~np.isnan(p["revenue"])
    pub = np.zeros_like(have)
    lag, lag_fy_days = (0, 0) if live else (LAG_DAYS, LAG_DAYS_FY)
    for j, e in enumerate(ends):
        lag_ok = when >= e + timedelta(days=lag)
        lag_fy = when >= e + timedelta(days=lag_fy_days)
        if derived is None:
            pub[:, j] = have[:, j] & lag_ok
        else:
            pub[:, j] = have[:, j] & np.where(derived[:, j], lag_fy, lag_ok)
    last = np.where(pub.any(axis=1), pub.shape[1] - 1 - np.argmax(pub[:, ::-1], axis=1), -1)
    newest = max((j for j, e in enumerate(ends) if when >= e + timedelta(days=lag or 1)), default=-1)
    return np.where(last >= newest - 1, last, -1)      # stale filers (two+ quarters behind) are skipped


# ── tier 2: prices and Yahoo facts for the shortlist ──────

@dataclass
class Px:
    close: float
    sma200: float | None
    ret6: float | None
    ret6_spy: float | None
    hi52: float | None
    dvol: float | None


def _px_facts(sym: str, bars: list[dict], spy) -> Px | None:
    c = [b["close"] for b in bars if b["close"] is not None]
    v = [b["volume"] or 0 for b in bars if b["close"] is not None]
    if len(c) < 60:
        return None
    sma200 = sum(c[-200:]) / 200 if len(c) >= 200 else None
    ret6 = c[-1] / c[-127] - 1 if len(c) >= 127 else None
    spy_r = None
    if spy and len(c) >= 127:
        d0, d1 = [b["day"] for b in bars if b["close"] is not None][-127], bars[-1]["day"]
        s0, s1 = spy.at(d0), spy.at(d1)
        spy_r = s1 / s0 - 1 if s0 and s1 else None
    dvol = sum(x * y for x, y in zip(c[-20:], v[-20:])) / 20
    return Px(c[-1], sma200, ret6, spy_r, max(c[-252:]), dvol)


def _yahoo(sym: str) -> dict:
    """Analyst estimates/revisions, rating changes (90 days), insider open-market buys (6 months), 3 headlines."""
    import yfinance as yf
    out: dict = {}
    tk = yf.Ticker(sym)
    try:
        out["mcap"] = float(tk.fast_info.get("marketCap") or 0) or None
    except Exception:  # noqa: BLE001
        pass
    try:
        ee = tk.earnings_estimate
        if ee is not None and "+1y" in ee.index:
            out["eps_next_y"] = float(ee.loc["+1y", "avg"])
            out["eps_this_y"] = float(ee.loc["0y", "avg"])
            out["n_analysts"] = int(ee.loc["+1y", "numberOfAnalysts"] or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        tr = tk.eps_trend
        if tr is not None and "+1y" in tr.index:
            cur, ago = float(tr.loc["+1y", "current"]), float(tr.loc["+1y", "90daysAgo"])
            out["eps_next_y_90d"] = ago
            out["rev_90d"] = (cur - ago) / abs(ago) * 100 if ago else None
    except Exception:  # noqa: BLE001
        pass
    try:
        rv = tk.eps_revisions
        if rv is not None and "+1y" in rv.index:
            out["rev_up30"] = int(rv.loc["+1y", "upLast30days"] or 0)
            out["rev_down30"] = int(rv.loc["+1y", "downLast30days"] or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        ud = tk.upgrades_downgrades
        if ud is not None and len(ud):
            cut = (date.today() - timedelta(days=90)).isoformat()
            recent = ud[[str(i)[:10] >= cut for i in ud.index]]
            ups = sum(1 for _, r in recent.iterrows() if r.get("Action") == "up" or r.get("priceTargetAction") == "Raises")
            downs = sum(1 for _, r in recent.iterrows() if r.get("Action") == "down" or r.get("priceTargetAction") == "Lowers")
            out["ratings_up90"], out["ratings_down90"] = ups, downs
    except Exception:  # noqa: BLE001
        pass
    try:
        it = tk.insider_transactions
        if it is not None and len(it):
            cut = (date.today() - timedelta(days=183)).isoformat()
            buys = [r for _, r in it.iterrows() if str(r.get("Start Date"))[:10] >= cut and "Purchase" in str(r.get("Text") or "")]
            out["insider_buys"] = len(buys)
            out["insider_buy_usd"] = float(sum((r.get("Value") or 0) for r in buys if r.get("Value") == r.get("Value")))
    except Exception:  # noqa: BLE001
        pass
    try:
        news = []
        for n in (tk.news or [])[:6]:
            c = n.get("content") or n
            title = c.get("title")
            url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")) if isinstance(c, dict) else None
            if title:
                news.append({"title": title, "day": (c.get("pubDate") or "")[:10], "url": url})
        out["news"] = news[:3]
    except Exception:  # noqa: BLE001
        pass
    return out


# ── live run ──────────────────────────────────────────────

def _fmt_q(label: str) -> str:
    return f"{label[7]}Ç {label[2:6]}"


def run(per_screen: int = 12, enrich: bool = True, refresh: bool = True, progress=None) -> dict:
    """Screen every filer, price the candidates, enrich the best per screen from Yahoo, store the run."""
    from . import config, prices
    if refresh:
        frames.refresh()
    p = frames.load_panel()
    if p is None:
        return {"error": "SEC verisi yok"}
    derived = getattr(p, "derived", None)
    aj = as_of_index(p, date.today(), derived, live=True)
    m = metrics(p, aj)
    masks = rules(m)
    cand = sorted({int(i) for mk in masks.values() for i in np.flatnonzero(mk)})
    syms = {i: p.names.get(int(p.ciks[i]), (None, None)) for i in cand}
    tickers = [t for t, _ in syms.values() if t]
    if progress:
        progress(f"{len(tickers)} aday için fiyat")
    start = (date.today() - timedelta(days=420)).isoformat()
    prices.refresh(tickers, start=date.fromisoformat(start))
    spy = prices.series(config.CALENDAR_SYMBOL)
    rows: list[dict] = []
    for i in cand:
        sym, name = syms[i]
        if not sym:
            continue
        bars = [b for b in prices.bars(sym, start) if b["close"] is not None]
        px = _px_facts(sym, bars, spy)
        if px is None or px.close < 3 or (px.dvol or 0) < 2e6:
            continue
        shares = m["shares"][i]
        mcap = px.close * shares if shares == shares and shares else None
        j = int(aj[i])
        for code, mk in masks.items():
            if not mk[i]:
                continue
            r = {"screen": code, "symbol": sym, "name": name, "cik": int(p.ciks[i]), "quarter": p.q[j], "quarter_tr": _fmt_q(p.q[j]),
                 "price": px.close, "mcap": mcap, "dvol": px.dvol, "trend_up": bool(px.sma200 and px.close > px.sma200),
                 "ret6": px.ret6, "ret6_spy": px.ret6_spy, "off_hi": (px.close / px.hi52 - 1) * 100 if px.hi52 else None,
                 "rev_yoy": _f(m["rev_yoy"][i]), "rev_yoy_prev": _f(m["rev_yoy_prev"][i]), "ni_yoy": _f(m["ni_yoy"][i]),
                 "ni_q": [_f(m["ni"][k][i]) for k in range(5)], "rev_q": [_f(m["rev"][k][i]) for k in range(5)],
                 "q_labels": [_fmt_q(p.q[j - k]) for k in range(5) if j - k >= 0],
                 "rev_ttm": _f(m["rev_ttm"][i]), "ni_ttm": _f(m["ni_ttm"][i]), "fscore": int(m["fscore"][i]),
                 "dil": _f(m["dil"][i]), "q_be": _f(m["q_be"][i]), "gm": _f(m["gm"][i]), "gm_prev": _f(m["gm_prev"][i]),
                 "cash": _f(m["cash"][i])}
            r["score"] = _score(r)
            rows.append(r)
    out: dict[str, list[dict]] = {}
    for code in SCREENS:
        rs = sorted([r for r in rows if r["screen"] == code], key=lambda r: -r["score"])
        out[code] = rs
    if enrich:
        for code, rs in out.items():
            for r in rs[:per_screen]:
                if progress:
                    progress(f"Yahoo {r['symbol']}")
                try:
                    r["y"] = _yahoo(r["symbol"])
                except Exception:  # noqa: BLE001
                    log.exception("yahoo facts %s failed", r["symbol"])
                    r["y"] = {}
                if r["mcap"] is None and r["y"].get("mcap"):
                    r["mcap"] = r["y"]["mcap"]
            # the shown names are re-ordered with what analysts expect (the rule result itself does not change)
            top = sorted(rs[:per_screen], key=lambda r: -(r["score"] + _analyst_bonus(r)))
            rs[:per_screen] = top
    counts = {code: len(rs) for code, rs in out.items()}
    run_row = {"created_at": utcnow_iso(), "day": date.today().isoformat(), "universe": int((aj >= 0).sum()),
               "candidates": len(cand), "counts": counts, "spy": spy.last()[1] if spy else None}
    _store(run_row, out, per_screen)
    return {**run_row, "lists": out}


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _score(r: dict) -> float:
    """Ordering inside a screen (not a forecast): rule strength + price trend (fundamental screens work better on
    stocks that the market has started to reward)."""
    s = 0.0
    if r["screen"] == "kara_yakin":
        s += 30 if (r["q_be"] or 99) <= 4 else 15 if (r["q_be"] or 99) <= 8 else 0
        s += min(30, (r["rev_yoy"] or 0) * 60)
    elif r["screen"] == "ilk_kar":
        s += min(40, (r["rev_yoy"] or 0) * 80)
    elif r["screen"] == "hizlanan":
        s += min(25, (r["ni_yoy"] or 0) * 20) + min(25, ((r["rev_yoy"] or 0) - (r["rev_yoy_prev"] or 0)) * 100)
    else:
        s += r["fscore"] * 6
    s += 20 if r["trend_up"] else 0
    if r["ret6"] is not None and r["ret6_spy"] is not None:
        s += max(-10, min(15, (r["ret6"] - r["ret6_spy"]) * 50))
    if r["off_hi"] is not None:
        s += 10 if r["off_hi"] > -15 else 0
    return round(s, 1)


def _analyst_bonus(r: dict) -> float:
    """Next-year EPS estimate above zero matters most for the loss-makers ("soon profitable"); upward estimate
    revisions and rating/target raises add a little for every list."""
    y = r.get("y") or {}
    b = 0.0
    eps = y.get("eps_next_y")
    if eps is not None:
        b += (25 if eps > 0 else -15) if r["screen"] == "kara_yakin" else (5 if eps > 0 else -5)
    if y.get("rev_90d") is not None:
        b += max(-10, min(10, y["rev_90d"] / 2))
    b += max(-5, min(5, (y.get("ratings_up90") or 0) - (y.get("ratings_down90") or 0)))
    return b


def _store(run_row: dict, lists: dict, per_screen: int) -> None:
    rid = db.execute("INSERT INTO idea_runs(created_at, day, body) VALUES (?,?,?)",
                     (run_row["created_at"], run_row["day"], json.dumps(run_row, ensure_ascii=False)))
    for code, rs in lists.items():
        for rank, r in enumerate(rs, 1):
            shown = rank <= per_screen
            body = r if shown else {k: r.get(k) for k in ("symbol", "name", "screen", "quarter_tr", "rev_yoy", "mcap", "trend_up")}
            db.execute("""INSERT INTO ideas(run_id, day, screen, symbol, rank, shown, price, score, body)
                          VALUES (?,?,?,?,?,?,?,?,?)""",
                       (rid, run_row["day"], code, r["symbol"], rank, 1 if shown else 0, r["price"], r["score"],
                        json.dumps(body, ensure_ascii=False, default=str)))


def latest() -> dict | None:
    r = db.query_one("SELECT * FROM idea_runs ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    run_row = json.loads(r["body"])
    lists: dict[str, list[dict]] = {code: [] for code in SCREENS}
    for x in db.query("SELECT * FROM ideas WHERE run_id = ? ORDER BY screen, rank", (r["id"],)):
        d = json.loads(x["body"])
        d.update(rank=x["rank"], shown=x["shown"])
        lists.setdefault(x["screen"], []).append(d)
    return {**run_row, "id": r["id"], "lists": lists}


def live_record(min_days: int = 20) -> dict:
    """How the shown picks did since the day they were first listed, vs SPY over the same days (one row per
    symbol + screen: its first listing)."""
    from . import config, prices
    spy = prices.series(config.CALENDAR_SYMBOL)
    rows = db.query("""SELECT screen, symbol, MIN(day) day, price FROM ideas WHERE shown = 1 GROUP BY screen, symbol""")
    out: dict[str, dict] = {}
    for code in SCREENS:
        rs = []
        for r in rows:
            if r["screen"] != code:
                continue
            last = prices.series(r["symbol"]).last()
            if not last or not r["price"] or (date.fromisoformat(last[0]) - date.fromisoformat(r["day"])).days < min_days:
                continue
            s0, s1 = spy.at(r["day"]), spy.at(last[0])
            ret = (last[1] / r["price"] - 1) * 100
            rs.append({"symbol": r["symbol"], "day": r["day"], "ret": ret, "spy": (s1 / s0 - 1) * 100 if s0 and s1 else None})
        beat = [x for x in rs if x["spy"] is not None]
        out[code] = {"n": len(rs), "avg": sum(x["ret"] for x in rs) / len(rs) if rs else None,
                     "beat": sum(1 for x in beat if x["ret"] > x["spy"]) / len(beat) * 100 if beat else None}
    return out


# ── backtest: every quarter since 2019, with only the filings public on that day ──

def _history_closes(min_day: int = 20180101) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    from . import lab_data
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for path in (lab_data.HIST_PATH, lab_data.SMALL_PATH, lab_data.EX_PATH):
        if not path.exists():
            continue
        z = np.load(path)
        for k in z.files:
            a = z[k]
            keep = a[:, 0] >= min_day
            out[lab_data._unkey(k)] = (a[keep, 0].astype(np.int64), a[keep, 4].astype(np.float32))
        z.close()
    return out


def _rebalance_days(first: date, last: date) -> list[date]:
    out, y = [], first.year
    while True:
        for m in (2, 5, 8, 11):
            d = date(y, m, 20)
            if first <= d <= last:
                out.append(d)
        y += 1
        if date(y, 1, 1) > last:
            return out


def backtest(horizons: tuple[int, ...] = (63, 126, 252), save: bool = True) -> dict:
    """Rebuild each screen four times a year (20 Feb/May/Aug/Nov ≈ 50 days after quarter end) from 2019, with the
    filings public on that day, and measure each pick's return over the next 3/6/12 months (sessions 63/126/252)
    against SPY and against the equal-weighted average of every priced filer on the same day.

    Universe for prices: today's S&P 500 + 400 + 600 members plus former S&P 500 members that still trade. Picks
    without prices are counted, not silently dropped: a pick whose company was delisted inside the horizon (free
    Sharadar ticker table, by CIK) enters the "gone" list, and `_range` reports the result as a range — as measured,
    with each gone pick at −30 % (Shumway 1997, average delisting return of performance delistings) and at −100 %."""
    from . import lab_data
    p = frames.load_panel()
    hist = _history_closes()
    dl = lab_data.delisting_by_cik()
    spy_d, spy_c = hist["SPY"]
    tick = {i: p.names.get(int(c), (None, None))[0] for i, c in enumerate(p.ciks)}
    priced = np.array([tick[i] in hist for i in range(len(p.ciks))])
    last_day = int(spy_d[-1])
    days = _rebalance_days(date(2019, 5, 20), date(last_day // 10000, last_day // 100 % 100, last_day % 100))
    res: dict = {code: {h: [] for h in horizons} for code in SCREENS}
    res_trend: dict = {code: {h: [] for h in horizons} for code in SCREENS}
    gone: dict = {code: {h: [] for h in horizons} for code in SCREENS}      # (date, spy return, universe average)
    alive: dict = {code: {h: 0 for h in horizons} for code in SCREENS}     # unpriced but still trading at the horizon
    unknown: dict = {code: {h: 0 for h in horizons} for code in SCREENS}   # unpriced, not in the ticker table
    uni: dict = {h: [] for h in horizons}
    per_day: list[dict] = []

    def fwd(sym, d0: int, h: int):
        ds, cs = hist[sym]
        i = int(np.searchsorted(ds, d0))
        if i >= len(ds) or i + h >= len(ds) or cs[i] < 5:
            return None, None
        tr = None
        if i >= 200:
            tr = bool(cs[i] > cs[i - 200:i].mean())
        return float(cs[i + h] / cs[i] - 1), tr

    for d in days:
        d0 = d.year * 10000 + d.month * 100 + d.day
        aj = as_of_index(p, d, p.derived)
        masks = rules(metrics(p, aj))
        si = int(np.searchsorted(spy_d, d0))
        row = {"day": d.isoformat()}
        for h in horizons:
            if si + h >= len(spy_d):
                continue
            spy_r = float(spy_c[si + h] / spy_c[si] - 1)
            u = [fwd(tick[i], d0, h)[0] for i in np.flatnonzero(priced & (aj >= 0))]
            u = [x for x in u if x is not None]
            if not u:
                continue
            u_avg = float(np.mean(u))
            uni[h].append((d.isoformat(), u_avg, spy_r, len(u)))
            end_day = int(spy_d[si + h])
            for code, mk in masks.items():
                for i in np.flatnonzero(mk & ~priced):
                    cik = int(p.ciks[i])
                    if cik not in dl:
                        unknown[code][h] += 1
                    elif dl[cik] is not None and d0 < dl[cik] <= end_day:
                        gone[code][h].append((d.isoformat(), spy_r, u_avg))
                    else:
                        alive[code][h] += 1
                for i in np.flatnonzero(mk & priced):
                    r, trend = fwd(tick[i], d0, h)
                    if r is None:
                        continue
                    rec = (d.isoformat(), tick[i], r, r - spy_r, r - u_avg)
                    res[code][h].append(rec)
                    if trend:
                        res_trend[code][h].append(rec)
            row[f"spy{h}"] = spy_r
            row[f"uni{h}"] = u_avg
        per_day.append(row)

    def summ(recs: list) -> dict:
        if not recs:
            return {"n": 0}
        r = np.array([x[2] for x in recs]) * 100
        ex_spy = np.array([x[3] for x in recs]) * 100
        ex_uni = np.array([x[4] for x in recs]) * 100
        yrs: dict[str, list] = {}
        for x in recs:
            yrs.setdefault(x[0][:4], []).append(x)
        return {"n": len(recs), "dates": len({x[0] for x in recs}), "avg": float(r.mean()), "median": float(np.median(r)),
                "beat_spy": float((ex_spy > 0).mean() * 100), "ex_spy": float(ex_spy.mean()), "ex_uni": float(ex_uni.mean()),
                "beat_uni": float((ex_uni > 0).mean() * 100), "p10": float(np.percentile(r, 10)), "p90": float(np.percentile(r, 90)),
                "years": {y: {"n": len(v), "ex_uni": float(np.mean([z[4] for z in v]) * 100),
                              "beat_spy": float(np.mean([z[3] > 0 for z in v]) * 100)} for y, v in sorted(yrs.items())}}

    def rng(code: str, h: int) -> dict:
        return _range([(x[3], x[4]) for x in res[code][h]], gone[code][h], alive[code][h], unknown[code][h])

    out = {"created_at": utcnow_iso(), "from": days[0].isoformat(), "to": days[-1].isoformat(), "dates": len(days),
           "horizons": list(horizons), "priced": int(priced.sum()),
           "universe": {str(h): {"n_dates": len(v), "avg": float(np.mean([x[1] for x in v]) * 100) if v else None,
                                 "spy": float(np.mean([x[2] for x in v]) * 100) if v else None} for h, v in uni.items()},
           "delisting_table": bool(dl),
           "screens": {code: {"all": {str(h): summ(res[code][h]) for h in horizons},
                              "trend": {str(h): summ(res_trend[code][h]) for h in horizons},
                              "range": {str(h): rng(code, h) for h in horizons}} for code in SCREENS}}
    if save:
        db.set_cursor(CURSOR_BT, json.dumps(out, ensure_ascii=False))
    return out


DELIST_RET = (-0.30, -1.0)


def _range(priced: list[tuple[float, float]], gone: list[tuple[str, float, float]], alive: int, unknown: int) -> dict:
    """PURE. Average excess return (%, vs SPY and vs the universe average) as measured and with the delisted picks put
    back at each DELIST_RET. priced = (excess vs SPY, excess vs universe) per priced pick; gone = (date, SPY return,
    universe average) per pick delisted inside the horizon; alive = unpriced picks still trading at the horizon end.

    Our prices cover only some survivors (index members), so gone picks are weighted by the share of surviving picks
    that are priced: w = priced / (priced + alive) — as if delisted picks had been in our price universe at that rate."""
    n = len(priced)
    out = {"n": n, "gone": len(gone), "alive_unpriced": alive, "unknown": unknown}
    if not n:
        return out
    w = n / (n + alive)
    es = np.array([x[0] for x in priced])
    eu = np.array([x[1] for x in priced])
    out |= {"w": w, "ex_spy": float(es.mean() * 100), "ex_uni": float(eu.mean() * 100)}
    for a in DELIST_RET:
        k = f"{int(round(-a * 100))}"
        gs = sum(a - g[1] for g in gone)
        gu = sum(a - g[2] for g in gone)
        den = n + w * len(gone)
        out[f"ex_spy_{k}"] = float((es.sum() + w * gs) / den * 100)
        out[f"ex_uni_{k}"] = float((eu.sum() + w * gu) / den * 100)
        out[f"ex_spy_{k}_all"] = float((es.sum() + gs) / (n + len(gone)) * 100)   # every gone pick at full weight (harshest)
    return out


def backtest_result() -> dict | None:
    r = db.get_cursor(CURSOR_BT)
    return json.loads(r) if r else None


def verdicts(bt: dict | None) -> dict[str, tuple[str, str]]:
    """PURE: one label per screen from its measured record (6 and 12 months vs the universe average)."""
    out = {}
    for code in SCREENS:
        s = ((bt or {}).get("screens") or {}).get(code) or {}
        a6, a12 = (s.get("all") or {}).get("126") or {}, (s.get("all") or {}).get("252") or {}
        if not a6.get("n") or a6["n"] < 30:
            out[code] = ("örnek az", "neu")
        elif a6["ex_uni"] >= 3 and (a12.get("ex_uni") or 0) >= 3:
            out[code] = ("ölçülmüş avantaj", "pos")
        elif a6["ex_uni"] <= 1:
            out[code] = ("ölçülmüş avantaj yok", "neg")
        else:
            out[code] = ("zayıf avantaj", "warn")
    return out
