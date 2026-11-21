"""Analyst rating changes and price targets (Yahoo upgrades_downgrades). Rule-based, no LLM."""

from __future__ import annotations

import logging
import math
from datetime import timedelta, timezone

import httpx

from .. import config, db, market
from ..util import iso, parse_dt, sha, utcnow

log = logging.getLogger(__name__)

ACTION_TR = {
    "up": "not yükseltti",
    "down": "not düşürdü",
    "init": "takibe başladı",
    "main": "notunu korudu",
    "reit": "notunu teyit etti",
}
BULLISH = {"buy", "strong buy", "outperform", "overweight", "accumulate", "positive", "market outperform",
           "sector outperform", "add", "conviction buy", "top pick", "long-term buy", "speculative buy"}
BEARISH = {"sell", "strong sell", "underperform", "underweight", "reduce", "negative", "market underperform",
           "sector underperform"}

KEEP_DAYS = 120   # history kept for the UI
FRESH_DAYS = 3    # older rows are stored as backfill (no alert)


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) or v == 0 else v
    except (TypeError, ValueError):
        return None


def classify(action: str, to_grade: str, pt_old: float | None, pt_new: float | None, pt_threshold: float) -> tuple[int, int, float | None]:
    """Return (importance 1-5, direction -1/0/1, pt change %)."""
    pt_chg = ((pt_new / pt_old - 1) * 100) if pt_old and pt_new else None
    g = (to_grade or "").strip().lower()
    if action == "up":
        return 4, 1, pt_chg
    if action == "down":
        return 4, -1, pt_chg
    if action == "init":
        return 3, (1 if g in BULLISH else -1 if g in BEARISH else 0), pt_chg
    if pt_chg is not None and abs(pt_chg) >= 0.5:
        imp = 3 if abs(pt_chg) >= pt_threshold else 2
        return imp, (1 if pt_chg > 0 else -1), pt_chg
    return 1, 0, pt_chg


def describe(sym: str, firm: str, action: str, from_g: str, to_g: str, pt_old, pt_new, pt_chg) -> str:
    grade = f"{from_g} → {to_g}" if from_g and to_g and from_g != to_g else (to_g or from_g or "")
    parts = [f"{firm} {ACTION_TR.get(action, action)}"]
    if grade:
        parts.append(f"({grade})")
    if pt_new:
        if pt_old and pt_old != pt_new:
            parts.append(f"· hedef {pt_old:g}$ → {pt_new:g}$ ({pt_chg:+.1f}%)")
        else:
            parts.append(f"· hedef {pt_new:g}$")
    return " ".join(parts)


_FMP_ACTION = {"upgrade": "up", "downgrade": "down", "initialize": "init", "initiate": "init",
               "maintain": "main", "reiterate": "reit"}


def _rows_yahoo(sym: str) -> list[dict]:
    df = market.upgrades_downgrades(sym)
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for ts, row in df.iterrows():
        when = ts.to_pydatetime()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        out.append({
            "when": when, "firm": str(row.get("Firm") or "").strip(),
            "action": str(row.get("Action") or "").strip().lower(),
            "from": str(row.get("FromGrade") or "").strip(), "to": str(row.get("ToGrade") or "").strip(),
            "pt_new": _f(row.get("currentPriceTarget")), "pt_old": _f(row.get("priorPriceTarget")),
        })
    return out


_fmp_blocked: dict[str, float] = {}


def _rows_fmp(sym: str) -> list[dict]:
    """Secondary source: catches rating changes Yahoo misses (no price targets).
    FMP's free plan only covers large caps (402 otherwise) — those symbols are skipped for a day."""
    import time as _t
    key = config.secret("FMP_API_KEY")
    if not key or _t.time() - _fmp_blocked.get(sym, 0) < 86400:
        return []
    r = httpx.get("https://financialmodelingprep.com/stable/grades", params={"symbol": sym, "apikey": key}, timeout=20)
    if r.status_code in (402, 403):
        _fmp_blocked[sym] = _t.time()
        return []
    if r.status_code >= 400:  # raise_for_status would put the URL (with the API key) into the log
        raise RuntimeError(f"FMP grades HTTP {r.status_code}")
    data = r.json() if isinstance(r.json(), list) else []
    out = []
    for g in data[:60]:
        when = parse_dt(g.get("date"))
        if not when:
            continue
        out.append({
            "when": when.replace(hour=12), "firm": (g.get("gradingCompany") or "").strip(),
            "action": _FMP_ACTION.get((g.get("action") or "").lower(), (g.get("action") or "").lower()),
            "from": (g.get("previousGrade") or "").strip(), "to": (g.get("newGrade") or "").strip(),
            "pt_new": None, "pt_old": None, "fmp": True,
        })
    return out


def _merge(yahoo: list[dict], fmp: list[dict]) -> list[dict]:
    """Yahoo first (has price targets); add FMP rows for (firm, day) pairs Yahoo lacks."""
    seen = {(r["firm"].lower(), r["when"].date()) for r in yahoo}
    extra = [r for r in fmp if (r["firm"].lower(), r["when"].date()) not in seen
             and r["action"] in ("up", "down", "init")]
    return yahoo + extra


def collect_ticker(ticker) -> int:
    sym = ticker["symbol"]
    backfill = ticker["backfilled_at"] is None
    rows: list[dict] = []
    try:
        rows = _rows_yahoo(sym)
    except Exception as exc:  # noqa: BLE001
        log.warning("ratings yahoo %s failed: %s", sym, exc)
    try:
        rows = _merge(rows, _rows_fmp(sym))
    except Exception as exc:  # noqa: BLE001
        log.warning("ratings fmp %s failed: %s", sym, exc)
    if not rows:
        return 0
    threshold = float(db.get_setting("rating_pt_change_pct") or 10)
    now = utcnow()
    keep_cut, fresh_cut = now - timedelta(days=KEEP_DAYS), now - timedelta(days=FRESH_DAYS)
    new_alerts = 0
    for row in rows:
        when = row["when"]
        if when < keep_cut:
            continue
        firm, action, from_g, to_g = row["firm"], row["action"], row["from"], row["to"]
        pt_new, pt_old = row["pt_new"], row["pt_old"]
        imp, direction, pt_chg = classify(action, to_g, pt_old, pt_new, threshold)
        title = describe(sym, firm, action, from_g, to_g, pt_old, pt_new, pt_chg)
        status = "backfill" if (backfill or when < fresh_cut) else "analyzed"
        eid = db.insert_event(
            type="rating",
            source=firm or "analist",
            symbol=sym,
            title=f"{sym}: {title}",
            published_at=iso(when),
            dedupe_key=sha("rating", sym, firm.lower(), when.date().isoformat(), action, to_g.lower(), str(pt_new)),
            status=status,
            importance=imp,
            direction=direction,
            extra={
                "firm": firm, "action": action, "from_grade": from_g, "to_grade": to_g,
                "pt_old": pt_old, "pt_new": pt_new, "pt_change_pct": round(pt_chg, 2) if pt_chg is not None else None,
            },
        )
        if eid and status == "analyzed":
            new_alerts += 1
    return new_alerts
