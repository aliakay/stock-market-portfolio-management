"""Earnings preview and post-mortem ("Bilanço").

Per symbol, all numbers are computed on the server from cached sources and
carry a source + time stamp:

- next report date and timing (Finnhub radar calendar, Yahoo earnings dates, FMP)
- EPS / revenue estimates (Yahoo analyst estimates, FMP), revision direction
- beat/miss history of the last 8 reports and the stock's reaction to each
  (close before the report vs close of the first session after it; the right
  session is picked from before-open / after-close timing)
- options-implied move for the first expiry after the report (ATM straddle,
  Yahoo chains: delayed, approximate)
- what the user has at stake: position, $ at risk, karne, open setup, thesis
- after the report (≤ 10 days): actual vs estimate, gap and next-day move,
  estimate revisions → "ne değişti"

Nothing here uses an LLM; missing inputs are reported as "doğrulanamadı".
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

from . import config, db, options, portfolio, prices, stockdata as sd
from .util import iso, parse_dt, tr_n, utcnow, utcnow_iso

log = logging.getLogger(__name__)

HISTORY_N = 8
POST_DAYS = 10            # a report this recent gets a post-mortem
TIMING_TR = {"bmo": "açılış öncesi", "amc": "kapanış sonrası", None: "saat belirsiz"}
DAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
DAYS_TR3 = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
NA = "doğrulanamadı"
_EPS_RE = re.compile(r"EPS beklentisi (-?\d+(?:\.\d+)?)")


# ── small helpers ─────────────────────────────────────────

def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _pct(v, d: int = 1, sign: bool = True) -> str:
    return "—" if not _isnum(v) else "%" + tr_n(v, d, sign=sign)


def _big(v) -> str:
    if not _isnum(v):
        return "—"
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{tr_n(v / div, 2)}{suf}$"
    return f"{tr_n(v, 0)}$"


def day_tr(d: str | None, short: bool = True) -> str:
    """'17.11 Sal' style."""
    if not d:
        return "—"
    x = date.fromisoformat(d[:10])
    return f"{x:%d.%m} {(DAYS_TR3 if short else DAYS_TR)[x.weekday()]}"


def _ny_now() -> datetime:
    return datetime.now(config.NY_TZ)


def next_weekday(d: str) -> str:
    x = date.fromisoformat(d) + timedelta(days=1)
    while x.weekday() >= 5:
        x += timedelta(days=1)
    return x.isoformat()


def reaction_day(report_day: str, timing: str | None) -> str:
    """First session that trades on the news: same day for before-open, next weekday otherwise."""
    if timing == "bmo":
        d = date.fromisoformat(report_day)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.isoformat()
    return next_weekday(report_day)


def timing_from_dt(ts: str | None) -> str | None:
    """Yahoo report timestamps: 16:00 ET → after close, before 09:30 → before open; else unknown."""
    if not ts or "T" not in ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(config.NY_TZ)
    m = dt.hour * 60 + dt.minute
    if m == 0:
        return None
    if m >= 16 * 60:
        return "amc"
    if m < 9 * 60 + 30:
        return "bmo"
    return None


def timing_from_name(name: str | None) -> str | None:
    n = name or ""
    return "amc" if "kapanış sonrası" in n else "bmo" if "açılış öncesi" in n else None


def report_dt(day: str, timing: str | None) -> datetime:
    """Approximate release moment (ET) used to tell 'upcoming' from 'reported'."""
    t = {"bmo": time(7, 0), "amc": time(16, 5)}.get(timing, time(16, 5))
    return datetime.combine(date.fromisoformat(day), t, tzinfo=config.NY_TZ)


def complete_bars(sym: str, start: str | None = None) -> list[dict]:
    """Daily bars without today's partial bar while the US session is still running."""
    bars = [b for b in prices.bars(sym, start) if b.get("close") is not None]
    now = _ny_now()
    if bars and bars[-1]["day"] == now.date().isoformat() and (now.hour, now.minute) < (16, 15):
        bars = bars[:-1]
    return bars


# ── pure calculations (unit-tested) ───────────────────────

def reaction(bars: list[dict], report_day: str, timing: str | None) -> dict | None:
    """Close before the report → first reaction session (gap at the open, close-to-close move) → next session.

    after close: pre = report day close, reaction = next session
    before open: pre = previous session close, reaction = report day
    unknown:     pre = previous session close, reaction = next session after the report day (2-session window)
    """
    days = [b["day"] for b in bars]
    if timing == "amc":
        pre_i = max((i for i, d in enumerate(days) if d <= report_day), default=None)
        post_i = pre_i + 1 if pre_i is not None else None
    elif timing == "bmo":
        pre_i = max((i for i, d in enumerate(days) if d < report_day), default=None)
        post_i = pre_i + 1 if pre_i is not None else None
    else:
        pre_i = max((i for i, d in enumerate(days) if d < report_day), default=None)
        post_i = next((i for i, d in enumerate(days) if d > report_day), None)
    if pre_i is None or post_i is None or post_i >= len(bars) or post_i <= pre_i:
        return None
    pre, post = bars[pre_i], bars[post_i]
    if not pre.get("close") or not post.get("close"):
        return None
    nxt = bars[post_i + 1] if post_i + 1 < len(bars) else None
    out = {"pre_day": pre["day"], "pre_close": pre["close"], "day": post["day"], "open": post.get("open"),
           "close": post["close"], "move_pct": (post["close"] / pre["close"] - 1) * 100,
           "gap_pct": (post["open"] / pre["close"] - 1) * 100 if post.get("open") else None,
           "intraday_pct": (post["close"] / post["open"] - 1) * 100 if post.get("open") else None,
           "next_day": nxt["day"] if nxt else None, "next_close": nxt["close"] if nxt else None,
           "next_pct": (nxt["close"] / post["close"] - 1) * 100 if nxt and nxt.get("close") else None,
           "two_day_pct": (nxt["close"] / pre["close"] - 1) * 100 if nxt and nxt.get("close") else None,
           "window": "2 seans" if timing is None else "1 seans"}
    return out


def history_stats(rows: list[dict]) -> dict:
    moves = [r["reaction"]["move_pct"] for r in rows if r.get("reaction")]
    eps = [r for r in rows if _beat(r) is not None]
    rev = [r for r in rows if _isnum(r.get("rev_act")) and _isnum(r.get("rev_est"))]
    big = max((r for r in rows if r.get("reaction")), key=lambda r: abs(r["reaction"]["move_pct"]), default=None)
    return {
        "n": len(rows), "n_moves": len(moves),
        "avg_abs": statistics.mean(abs(m) for m in moves) if moves else None,
        "median_abs": statistics.median(abs(m) for m in moves) if moves else None,
        "avg": statistics.mean(moves) if moves else None,
        "up": sum(1 for m in moves if m > 0), "down": sum(1 for m in moves if m < 0),
        "max": big["reaction"]["move_pct"] if big else None, "max_day": big["date"] if big else None,
        "eps_n": len(eps), "beats": sum(1 for r in eps if _beat(r) == 1),
        "misses": sum(1 for r in eps if _beat(r) == -1),
        "rev_n": len(rev), "rev_beats": sum(1 for r in rev if r["rev_act"] > r["rev_est"]),
        "beat_up": sum(1 for r in eps if _beat(r) == 1 and r.get("reaction") and r["reaction"]["move_pct"] > 0),
        "beat_n": sum(1 for r in eps if _beat(r) == 1 and r.get("reaction")),
    }


def implied_move(chain: dict | None, after_day: str) -> dict | None:
    """ATM straddle of the first expiry on/after `after_day` (the reaction session).

    Straddle ≈ the market's expected absolute move to expiry; using the first
    expiry after the report keeps most of that premium earnings-driven.
    Falls back to ATM IV × √t when quotes are missing.
    """
    if not chain or not chain.get("spot") or not chain.get("expiries"):
        return None
    spot = chain["spot"]
    exps = sorted((e for e in chain["expiries"] if e.get("expiry", "") >= after_day), key=lambda e: e["expiry"])
    for e in exps:
        calls = [c for c in e.get("calls") or [] if c.get("strike")]
        puts = [p for p in e.get("puts") or [] if p.get("strike")]
        common = {c["strike"] for c in calls} & {p["strike"] for p in puts}
        if not common:
            continue
        k = min(common, key=lambda s: abs(s - spot))
        c = next(r for r in calls if r["strike"] == k)
        p = next(r for r in puts if r["strike"] == k)
        cm, pm = options._mid(c), options._mid(p)
        quoted = all(r.get("bid") and r.get("ask") and r["ask"] >= r["bid"] > 0 for r in (c, p))
        days = max(1, (date.fromisoformat(e["expiry"]) - date.fromisoformat(after_day)).days + 1)
        ivs = [x for x in (c.get("iv"), p.get("iv")) if x and 0.01 < x < 5]
        iv = sum(ivs) / len(ivs) if ivs else None
        if cm and pm:
            move, method = cm + pm, "ATM straddle (" + ("alış/satış ortası" if quoted else "son işlem fiyatı, kotasyon yok") + ")"
        elif iv:
            move, method = spot * iv * ((days / 365) ** 0.5), "ATM IV × √süre"
        else:
            continue
        return {"expiry": e["expiry"], "strike": k, "spot": spot, "move": move, "pct": move / spot * 100,
                "iv": iv, "method": method, "call_mid": cm, "put_mid": pm,
                "days_after": (date.fromisoformat(e["expiry"]) - date.fromisoformat(after_day)).days,
                "diluted": (date.fromisoformat(e["expiry"]) - date.fromisoformat(after_day)).days > 7}
    return None


def revision(trend: list[dict] | None, period: str = "0q") -> dict | None:
    """Estimate drift: % change when both ends are positive, otherwise the $ difference (loss-makers)."""
    row = next((r for r in trend or [] if r.get("period") == period), None)
    if not row or not _isnum(row.get("current")):
        return None
    cur = row["current"]

    def chg(col):
        v = row.get(col)
        return (cur / v - 1) * 100 if _isnum(v) and v > 0 and cur > 0 else None

    def diff(col):
        v = row.get(col)
        return cur - v if _isnum(v) else None

    out = {"period": period, "current": cur, "d7": row.get("7daysAgo"), "d30": row.get("30daysAgo"), "d90": row.get("90daysAgo"),
           "chg7": chg("7daysAgo"), "chg30": chg("30daysAgo"), "chg90": chg("90daysAgo"),
           "diff7": diff("7daysAgo"), "diff30": diff("30daysAgo"), "diff90": diff("90daysAgo")}
    if out["chg30"] is not None:
        ref, eps_ = out["chg30"], 0.5
    elif out["diff30"] is not None:
        ref, eps_ = out["diff30"], 0.005
    else:
        ref, eps_ = None, 0
    out["direction"] = None if ref is None else "yukarı" if ref > eps_ else "aşağı" if ref < -eps_ else "yatay"
    return out


def rev_text(r: dict | None, days: int = 7) -> str:
    """'%+3,0' or '+0,31$' for the change over `days` (7/30/90)."""
    if not r:
        return NA
    c, d = r.get(f"chg{days}"), r.get(f"diff{days}")
    if c is not None:
        return _pct(c)
    if d is not None:
        return tr_n(d, 2, sign=True) + "$"
    return "—"


def _beat(r: dict) -> int | None:
    """1 beat, -1 miss, 0 in line; Yahoo surprise % is more precise than the rounded EPS values."""
    if _isnum(r.get("surprise")):
        return 1 if r["surprise"] > 0.05 else -1 if r["surprise"] < -0.05 else 0
    if _isnum(r.get("eps_act")) and _isnum(r.get("eps_est")):
        return 1 if r["eps_act"] > r["eps_est"] else -1 if r["eps_act"] < r["eps_est"] else 0
    return None


# ── inputs ────────────────────────────────────────────────

def _cache_only(key: str, source: str) -> sd.Fetched:
    row = db.query_one("SELECT * FROM api_cache WHERE key = ?", (key,))
    if not row:
        return sd.Fetched(None, None, source)
    return sd.Fetched(json.loads(row["body"]), row["fetched_at"], row["source"] or source,
                      stale=utcnow() - parse_dt(row["fetched_at"]) > timedelta(hours=36))


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        log.warning("earnings input %s failed: %s", getattr(fn, "__name__", fn), exc)
        return None


def inputs(sym: str, *, quick: bool = False) -> dict:
    """quick=True (list pages): FMP and option chains only from cache (FMP free plan 429s on bursts)."""
    sym = sym.upper()
    fmp_key = f"fmp:earnings?limit=5&symbol={sym}"
    x = {
        "ed": _safe(sd.yahoo, "earnings_dates", sym, 12),
        "est": _safe(sd.yahoo, "earnings_estimate", sym),
        "rev": _safe(sd.yahoo, "revenue_estimate", sym),
        "trend": _safe(sd.yahoo, "eps_trend", sym),
        "fmp": _cache_only(fmp_key, "FMP") if quick else _safe(sd.fmp, "earnings", 24, symbol=sym, limit=5),
        "chain": _cache_only(f"yahoo:options:{sym}:60", "Yahoo opsiyon zinciri") if quick else _safe(sd.option_chains, sym),
        "cal": [dict(r) for r in db.query("SELECT * FROM macro_calendar WHERE key = ? ORDER BY release_at", (f"earnings:{sym}",))],
    }
    if not x["ed"] and not x["cal"]:
        x["ycal"] = _safe(sd.yahoo, "calendar", sym, 12)
    return x


def _d(f, default=None):
    return f.data if f and f.data not in (None, [], {}) else default


# ── assembling ────────────────────────────────────────────

def _reports(sym: str, x: dict) -> tuple[list[dict], list[dict]]:
    """(upcoming candidates, past reports newest first) from Yahoo dates + FMP + radar calendar."""
    now = _ny_now()
    past, upcoming = [], []
    fmp_rows = _d(x.get("fmp"), []) or []
    cal = x.get("cal") or []
    cal_by_day = {}
    for c in cal:
        d = parse_dt(c["release_at"]).astimezone(config.NY_TZ)
        cal_by_day[d.date().isoformat()] = c

    def fmp_match(day):
        best = None
        for r in fmp_rows:
            if not r.get("date"):
                continue
            gap = abs((date.fromisoformat(r["date"][:10]) - date.fromisoformat(day)).days)
            if gap <= 3 and (best is None or gap < best[0]):
                best = (gap, r)
        return best[1] if best else None

    def cal_timing(day):
        c = cal_by_day.get(day)
        return timing_from_name(c["name"]) if c else None

    seen = set()
    for r in _d(x.get("ed"), []) or []:
        ts = r.get("index") or r.get("Earnings Date")
        if not ts:
            continue
        day = ts[:10]
        if day in seen:
            continue
        seen.add(day)
        timing = timing_from_dt(ts) or cal_timing(day)
        fm = fmp_match(day)
        row = {"date": day, "timing": timing, "eps_est": r.get("EPS Estimate"), "eps_act": r.get("Reported EPS"),
               "surprise": r.get("Surprise(%)"), "rev_est": (fm or {}).get("revenueEstimated"),
               "rev_act": (fm or {}).get("revenueActual"), "src": "Yahoo"}
        if row["eps_act"] is None and report_dt(day, timing) > now:
            upcoming.append(row)
        elif row["eps_act"] is not None or report_dt(day, timing) <= now:
            past.append(row)
    if not past and not upcoming:  # Yahoo dates missing: FMP rows
        for r in fmp_rows:
            day = (r.get("date") or "")[:10]
            if not day:
                continue
            row = {"date": day, "timing": cal_timing(day), "eps_est": r.get("epsEstimated"), "eps_act": r.get("epsActual"),
                   "surprise": None, "rev_est": r.get("revenueEstimated"), "rev_act": r.get("revenueActual"), "src": "FMP"}
            if _isnum(row["eps_act"]) and _isnum(row["eps_est"]) and row["eps_est"]:
                row["surprise"] = (row["eps_act"] - row["eps_est"]) / abs(row["eps_est"]) * 100
            (upcoming if row["eps_act"] is None and day >= now.date().isoformat() else past).append(row)
    for r in past:
        if r["surprise"] is None and _isnum(r["eps_act"]) and _isnum(r["eps_est"]) and r["eps_est"]:
            r["surprise"] = (r["eps_act"] - r["eps_est"]) / abs(r["eps_est"]) * 100
        r["rev_surprise"] = ((r["rev_act"] / r["rev_est"] - 1) * 100
                             if _isnum(r.get("rev_act")) and _isnum(r.get("rev_est")) and r["rev_est"] else None)
    past.sort(key=lambda r: r["date"], reverse=True)
    upcoming.sort(key=lambda r: r["date"])
    return upcoming, past


def _next(sym: str, x: dict, upcoming: list[dict]) -> dict | None:
    cands = []
    for c in x.get("cal") or []:
        when = parse_dt(c["release_at"])
        d = when.astimezone(config.NY_TZ).date().isoformat()
        timing = timing_from_name(c["name"])
        if report_dt(d, timing) <= datetime.now(config.NY_TZ) or c.get("released"):
            continue
        m = _EPS_RE.search(c["name"] or "")
        cands.append({"date": d, "timing": timing, "src": "Finnhub (radar takvimi)", "eps": float(m.group(1)) if m else None,
                      "release_at": c["release_at"]})
        break
    if upcoming:
        u = upcoming[0]
        cands.append({"date": u["date"], "timing": u["timing"], "src": "Yahoo", "eps": u["eps_est"]})
    for r in sorted(_d(x.get("fmp"), []) or [], key=lambda r: r.get("date") or ""):
        if r.get("epsActual") is None and (r.get("date") or "") >= datetime.now(config.NY_TZ).date().isoformat():
            cands.append({"date": r["date"][:10], "timing": None, "src": "FMP", "eps": r.get("epsEstimated"),
                          "rev": r.get("revenueEstimated")})
            break
    yc = _d(x.get("ycal"), {}) or {}
    if not cands and yc.get("Earnings Date"):
        d = yc["Earnings Date"][0][:10]
        if d >= datetime.now(config.NY_TZ).date().isoformat():
            cands.append({"date": d, "timing": None, "src": "Yahoo takvim", "eps": yc.get("Earnings Average"),
                          "rev": yc.get("Revenue Average")})
    if not cands:
        return None
    p = dict(cands[0])
    if p["timing"] is None:
        p["timing"] = next((c["timing"] for c in cands[1:] if c["timing"] and c["date"] == p["date"]), None)
    conflicts = [c for c in cands[1:] if abs((date.fromisoformat(c["date"]) - date.fromisoformat(p["date"])).days) > 1]
    if not p.get("release_at"):
        p["release_at"] = iso(report_dt(p["date"], p["timing"]).astimezone(config.LOCAL_TZ)) if p["timing"] else None
    today = datetime.now(config.NY_TZ).date()
    p.update(timing_tr=TIMING_TR[p["timing"]], day_tr=day_tr(p["date"], short=False),
             days_to=(date.fromisoformat(p["date"]) - today).days,
             reaction_day=reaction_day(p["date"], p["timing"]),
             others=[{"src": c["src"], "date": c["date"], "eps": c.get("eps")} for c in cands[1:]],
             conflict=[f"{c['src']}: {c['date']}" for c in conflicts])
    p["fmp_eps"] = next((c["eps"] for c in cands if c["src"] == "FMP"), None)
    p["fmp_rev"] = next((c.get("rev") for c in cands if c["src"] == "FMP"), None)
    p["finnhub_eps"] = next((c["eps"] for c in cands if c["src"].startswith("Finnhub")), None)
    return p


def _estimates(x: dict) -> dict:
    def row(f, period):
        return next((r for r in _d(f, []) or [] if r.get("period") == period), {})
    e0, r0 = row(x.get("est"), "0q"), row(x.get("rev"), "0q")
    return {"eps": e0.get("avg"), "eps_lo": e0.get("low"), "eps_hi": e0.get("high"), "eps_n": e0.get("numberOfAnalysts"),
            "eps_year_ago": e0.get("yearAgoEps"), "eps_growth": e0["growth"] * 100 if _isnum(e0.get("growth")) else None,
            "rev": r0.get("avg"), "rev_lo": r0.get("low"), "rev_hi": r0.get("high"), "rev_n": r0.get("numberOfAnalysts"),
            "rev_year_ago": r0.get("yearAgoRevenue"), "rev_growth": r0["growth"] * 100 if _isnum(r0.get("growth")) else None,
            "rev_q": revision(_d(x.get("trend")), "0q"), "rev_y": revision(_d(x.get("trend")), "0y"),
            "next_q": revision(_d(x.get("trend")), "+1q")}


def _implied_key(sym: str, day: str) -> str:
    return f"earn:implied:{sym}:{day}"


def _store_implied(sym: str, day: str, im: dict, fetched_at: str | None) -> None:
    """Remember the last pre-report implied move so the post-mortem can compare it with the real move."""
    try:
        body = json.dumps(im | {"chain_at": fetched_at})
        db.execute("INSERT INTO api_cache(key, source, fetched_at, body) VALUES (?,?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body",
                   (_implied_key(sym, day), "radar", utcnow_iso(), body))
    except Exception as exc:  # noqa: BLE001
        log.warning("implied snapshot %s: %s", sym, exc)


def _stored_implied(sym: str, day: str) -> dict | None:
    row = db.query_one("SELECT body, fetched_at FROM api_cache WHERE key = ?", (_implied_key(sym, day),))
    return json.loads(row["body"]) | {"saved_at": row["fetched_at"]} if row else None


def exposure(sym: str, implied_pct: float | None, avg_abs: float | None, positions: list[dict] | None = None) -> dict:
    pos = positions if positions is not None else (_safe(portfolio.positions) or [])
    stock = [p for p in pos if p["symbol"] == sym and p["asset"] != "option"]
    opts = []
    for p in pos:
        if p["asset"] == "option":
            o = portfolio.parse_occ(p["symbol"])
            if o and o["underlying"] == sym:
                opts.append(p | o)
    qty = sum(p["qty"] for p in stock)
    value = sum(p["value"] or 0 for p in stock)
    weight = sum(p["weight"] or 0 for p in stock)
    out = {"held": bool(stock or opts), "qty": qty, "value": value, "weight": weight,
           "avg": (sum(p["cost"] for p in stock) / qty) if qty else None,
           "unreal_pct": stock[0]["unreal_pct"] if len(stock) == 1 else None,
           "price": stock[0]["price"] if stock else None, "price_day": stock[0]["price_day"] if stock else None,
           "subs": sorted({p["sub"] for p in stock if p.get("sub")}),
           "options": [{"symbol": o["symbol"], "type": "CALL" if o["opt_type"] == "C" else "PUT", "strike": o["strike"],
                        "expiry": o["expiry"], "qty": o["qty"], "value": o["value"]} for o in opts],
           "options_value": sum(o["value"] or 0 for o in opts)}
    out["risk_implied"] = value * implied_pct / 100 if value and _isnum(implied_pct) else None
    out["risk_hist"] = value * avg_abs / 100 if value and _isnum(avg_abs) else None
    return out


def _context(sym: str) -> dict:
    from . import scorecard, setups
    out = {"card": None, "setups": [], "thesis": None}
    c = _safe(scorecard.latest, sym)
    if c:
        out["card"] = {"overall": c.get("overall"), "label": c.get("label"), "tone": c.get("tone"), "created_at": c.get("created_at")}
    out["setups"] = [s for s in (_safe(setups.open_setups) or []) if s["symbol"] == sym]
    try:
        from . import thesis
        out["thesis"] = thesis.for_symbol(sym)
    except Exception as exc:  # noqa: BLE001
        log.warning("thesis %s: %s", sym, exc)
    return out


def _post(sym: str, last: dict | None, est: dict, today: date) -> dict | None:
    if not last or (today - date.fromisoformat(last["date"])).days > POST_DAYS:
        return None
    p = dict(last)
    p["days_ago"] = (today - date.fromisoformat(last["date"])).days
    p["timing_tr"] = TIMING_TR[last["timing"]]
    p["implied_before"] = _stored_implied(sym, last["date"])
    r = last.get("reaction")
    lines = []
    if _isnum(p["eps_act"]) and _isnum(p["eps_est"]):
        verdict = {1: "aştı", -1: "kaçırdı", 0: "tutturdu"}[_beat(p)]
        lines.append(f"EPS {tr_n(p['eps_act'])}$ ile beklentiyi ({tr_n(p['eps_est'])}$) {verdict}"
                     + (f" ({_pct(p['surprise'])})" if _isnum(p.get("surprise")) else "") + ".")
    else:
        lines.append(f"Açıklanan EPS {NA}.")
    if _isnum(p.get("rev_act")) and _isnum(p.get("rev_est")):
        lines.append(f"Gelir {_big(p['rev_act'])}, beklenti {_big(p['rev_est'])} ({_pct(p['rev_surprise'])}).")
    if r:
        lines.append(f"Hisse ilk seansta ({day_tr(r['day'])}) açılışta {_pct(r['gap_pct'])} boşluk, kapanışta {_pct(r['move_pct'])}"
                     + (f"; ertesi gün {_pct(r['next_pct'])}" if _isnum(r.get("next_pct")) else "") + ".")
    else:
        lines.append(f"Fiyat tepkisi {NA} (seans kapanışı henüz yok).")
    ib = p["implied_before"]
    if r and ib and _isnum(ib.get("pct")):
        ratio = abs(r["move_pct"]) / ib["pct"] if ib["pct"] else None
        lines.append(f"Opsiyonlar ±%{tr_n(ib['pct'], 1)} fiyatlamıştı; gerçekleşen hareket bunun {tr_n(ratio, 1)} katı.")
    rq = est.get("rev_q")
    if rq and rq.get("diff7") is not None:
        lines.append(f"Rapordan sonra sıradaki çeyrek EPS tahmini son 7 günde {rev_text(rq, 7)} "
                     f"({tr_n(rq['d7'])} → {tr_n(rq['current'])}$).")
    ry = est.get("rev_y")
    if ry and ry.get("diff7") is not None:
        lines.append(f"Bu mali yıl EPS tahmini son 7 günde {rev_text(ry, 7)} ({tr_n(ry['d7'])} → {tr_n(ry['current'])}$).")
    p["what_changed"] = lines
    p["tone"] = ("pos" if r and r["move_pct"] > 0 else "neg" if r and r["move_pct"] < 0 else "neu")
    return p


def build(symbol: str, *, quick: bool = False, positions: list[dict] | None = None, x: dict | None = None) -> dict:
    sym = symbol.upper()
    x = x or inputs(sym, quick=quick)
    upcoming, past = _reports(sym, x)
    bars = complete_bars(sym, (date.today() - timedelta(days=3 * 366)).isoformat())
    for r in past[:HISTORY_N]:
        r["reaction"] = reaction(bars, r["date"], r["timing"])
        r["timing_tr"] = TIMING_TR[r["timing"]]
    hist = past[:HISTORY_N]
    stats = history_stats(hist)
    nxt = _next(sym, x, upcoming)
    est = _estimates(x)
    chain = _d(x.get("chain"))
    implied = None
    if nxt:
        implied = implied_move(chain, nxt["reaction_day"])
        if implied and not quick and nxt["days_to"] >= 0:
            _store_implied(sym, nxt["date"], implied, x["chain"].fetched_at if x.get("chain") else None)
        if implied and _isnum(stats["avg_abs"]) and stats["avg_abs"] > 0:
            implied["vs_hist"] = implied["pct"] / stats["avg_abs"]
    today = datetime.now(config.NY_TZ).date()
    post = _post(sym, past[0] if past else None, est, today)
    if post and not post.get("reaction"):
        post["reaction"] = reaction(bars, post["date"], post["timing"])
    exp = exposure(sym, implied["pct"] if implied else None, stats["avg_abs"], positions)
    ctx = _context(sym) if not quick else {"card": None, "setups": [], "thesis": None}
    if quick:
        try:
            from . import scorecard
            c = scorecard.latest(sym)
            ctx["card"] = c and {"overall": c.get("overall"), "label": c.get("label"), "tone": c.get("tone"), "created_at": c.get("created_at")}
        except Exception:  # noqa: BLE001
            pass

    missing = []
    if not nxt:
        missing.append("sıradaki bilanço tarihi")
    if nxt and not implied:
        missing.append("opsiyon ima edilen hareket" + (" (zincir önbellekte yok)" if quick else ""))
    if not stats["n_moves"]:
        missing.append("geçmiş fiyat tepkisi")
    if est["eps"] is None and not (nxt and nxt.get("eps") is not None):
        missing.append("EPS beklentisi")

    sources = [sd.stamp(x.get("ed"), "Yahoo bilanço tarihleri"), sd.stamp(x.get("est"), "Yahoo tahmin"),
               sd.stamp(x.get("trend"), "Yahoo revizyon"), sd.stamp(x.get("fmp"), "FMP bilanço")]
    if x.get("cal"):
        sources.append("Finnhub takvim (radar)")
    if x.get("chain"):
        sources.append(sd.stamp(x["chain"], "Yahoo opsiyon zinciri"))
    lb = bars[-1]["day"] if bars else None
    sources.append(f"Yahoo günlük kapanış · T {lb}" if lb else "Yahoo günlük kapanış · yok")

    out = {"symbol": sym, "name": _name(sym), "next": nxt, "est": est, "history": hist, "stats": stats, "implied": implied,
           "post": post, "exposure": exp, "ctx": ctx, "missing": missing, "sources": [s for s in sources if s],
           "last_bar": lb, "built_at": datetime.now(config.LOCAL_TZ), "quick": quick,
           "chain_stamp": sd.stamp(x["chain"], "Yahoo opsiyon") if x.get("chain") else None}
    out["summary"] = summary(out)
    return out


def _name(sym: str) -> str:
    t = db.get_ticker(sym)
    return (t["short_name"] or t["name"]) if t and (t["short_name"] or t["name"]) else sym


# ── text ──────────────────────────────────────────────────

def summary(b: dict) -> str:
    n, st, im, e, ex = b["next"], b["stats"], b["implied"], b["est"], b["exposure"]
    parts = []
    if n:
        when = "bugün" if n["days_to"] == 0 else "yarın" if n["days_to"] == 1 else f"{n['days_to']} gün sonra"
        parts.append(f"{b['symbol']} bilançosu {day_tr(n['date'], short=False)} {n['timing_tr']} ({when}).")
        eps = e["eps"] if e["eps"] is not None else n.get("eps")
        if eps is not None:
            parts.append(f"EPS beklentisi {tr_n(eps)}$" + (f" ({e['eps_n']} analist)" if e.get("eps_n") else "")
                         + (f", gelir {_big(e['rev'])}" if e.get("rev") else "") + ".")
        if im:
            parts.append(f"Opsiyonlar {im['expiry']} vadesine ±%{tr_n(im['pct'], 1)} (±{tr_n(im['move'])}$) hareket fiyatlıyor.")
    else:
        parts.append(f"{b['symbol']} için sıradaki bilanço tarihi {NA}.")
    if st["n_moves"]:
        parts.append(f"Son {st['n_moves']} raporda ortalama hareket ±%{tr_n(st['avg_abs'], 1)} ({st['up']} yukarı, {st['down']} aşağı)"
                     + (f"; {st['beats']}/{st['eps_n']} raporda EPS beklentisi aşıldı" if st["eps_n"] else "") + ".")
    if ex["held"] and ex["value"]:
        risk = ex["risk_implied"] if ex["risk_implied"] is not None else ex["risk_hist"]
        basis = "opsiyon fiyatlamasıyla" if ex["risk_implied"] is not None else "geçmiş ortalamayla"
        parts.append(f"Pozisyonun {tr_n(ex['value'], 0)}$" + (f": {basis} ±{tr_n(risk, 0)}$ oynayabilir." if risk else "."))
    return " ".join(parts)


def brief(symbol: str, b: dict | None = None) -> dict:
    """Compact dict for the Hermes agent / context snapshot."""
    b = b or build(symbol)
    rd = lambda v, d=2: None if not _isnum(v) else round(v, d)  # noqa: E731
    n, im, st, ex, p = b["next"], b["implied"], b["stats"], b["exposure"], b["post"]
    return {
        "symbol": b["symbol"],
        "next_report": n and {"date": n["date"], "timing": n["timing"], "timing_tr": n["timing_tr"], "days_to": n["days_to"],
                              "source": n["src"], "eps_est": rd(b["est"]["eps"] if b["est"]["eps"] is not None else n.get("eps"), 4),
                              "rev_est": rd(b["est"]["rev"], 0), "analysts": b["est"]["eps_n"], "date_conflict": n["conflict"]},
        "implied_move": im and {"expiry": im["expiry"], "pct": rd(im["pct"]), "usd_per_share": rd(im["move"]), "method": im["method"],
                                "approx": True, "vs_hist_avg": rd(im.get("vs_hist"))},
        "history": {"reports": st["n"], "beats": st["beats"], "misses": st["misses"], "eps_n": st["eps_n"],
                    "avg_abs_move_pct": rd(st["avg_abs"]), "up": st["up"], "down": st["down"], "max_move_pct": rd(st["max"]),
                    "last": [{"date": r["date"], "eps_est": rd(r["eps_est"], 4), "eps_act": rd(r["eps_act"], 4),
                              "surprise_pct": rd(r["surprise"]), "move_pct": rd((r.get("reaction") or {}).get("move_pct"))}
                             for r in b["history"][:4]]},
        "revision_0q": b["est"]["rev_q"] and {"direction": b["est"]["rev_q"]["direction"], "chg7_pct": rd(b["est"]["rev_q"]["chg7"]),
                                               "chg30_pct": rd(b["est"]["rev_q"]["chg30"]), "diff30_usd": rd(b["est"]["rev_q"]["diff30"], 4),
                                               "current": rd(b["est"]["rev_q"]["current"], 4)},
        "position": {"held": ex["held"], "qty": rd(ex["qty"], 4), "value_usd": rd(ex["value"], 0), "weight_pct": rd(ex["weight"], 1),
                     "at_risk_implied_usd": rd(ex["risk_implied"], 0), "at_risk_hist_usd": rd(ex["risk_hist"], 0),
                     "options": ex["options"]} if ex["held"] else None,
        "post_mortem": p and {"date": p["date"], "eps_est": rd(p["eps_est"], 4), "eps_act": rd(p["eps_act"], 4),
                              "surprise_pct": rd(p["surprise"]), "rev_surprise_pct": rd(p.get("rev_surprise")),
                              "gap_pct": rd((p.get("reaction") or {}).get("gap_pct")), "move_pct": rd((p.get("reaction") or {}).get("move_pct")),
                              "next_day_pct": rd((p.get("reaction") or {}).get("next_pct")), "what_changed": p["what_changed"]},
        "summary": b["summary"], "missing": b["missing"], "sources": b["sources"],
    }


# ── lists ─────────────────────────────────────────────────

def universe() -> list[tuple[str, bool]]:
    """(symbol, held) for holdings (stocks + option underlyings) and the active watchlist."""
    pos = _safe(portfolio.positions) or []
    held = set()
    for p in pos:
        if p["asset"] == "option":
            o = portfolio.parse_occ(p["symbol"])
            if o:
                held.add(o["underlying"])
        elif p["asset"] in ("stock", "etf"):
            held.add(p["symbol"])
    syms = set(held) | {t["symbol"] for t in db.active_tickers()}
    return sorted(((s, s in held) for s in syms), key=lambda t: (not t[1], t[0]))


def upcoming(days: int = 14, *, quick: bool = True, held_only: bool = False) -> dict:
    """Reports in the next `days` days (holdings first, then by date) and post-mortems of the last 10 days."""
    pos = _safe(portfolio.positions) or []
    uni = [u for u in universe() if u[1] or not held_only]

    def one(u):
        sym, held = u
        try:
            b = build(sym, quick=quick, positions=pos)
        except Exception as exc:  # noqa: BLE001
            log.warning("earnings build %s: %s", sym, exc)
            return None
        b["held"] = held
        return b

    with ThreadPoolExecutor(max_workers=4) as ex:
        built = [b for b in ex.map(one, uni) if b]
    soon = [b for b in built if b["next"] and 0 <= b["next"]["days_to"] <= days]
    soon.sort(key=lambda b: (not b["held"], b["next"]["date"], b["next"]["timing"] != "bmo", b["symbol"]))
    later = sorted((b for b in built if b["next"] and b["next"]["days_to"] > days), key=lambda b: b["next"]["date"])
    unknown = [b["symbol"] for b in built if not b["next"]]
    recent = sorted((b for b in built if b["post"]), key=lambda b: b["post"]["date"], reverse=True)
    return {"days": days, "soon": soon, "later": later, "unknown": unknown, "recent": recent,
            "built_at": datetime.now(config.LOCAL_TZ), "n": len(built)}
