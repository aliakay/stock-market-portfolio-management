"""One market report at a single as-of day T (the last SPY bar): regime + pulse + rotation + breadth + track record."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import breadth, config, db, pulse, regime, rotation

_cache: dict = {}
_lock = threading.Lock()
TTL = 300


def build(force: bool = False) -> dict:
    with _lock:
        if not force and _cache.get("at") and time.time() - _cache["at"] < TTL:
            return _cache["rep"]
        rep = _build()
        _cache.update(at=time.time(), rep=rep)
        return rep


def _build() -> dict:
    m = regime.load()
    if not m.days:
        return {"day": None, "regime": None, "pulse": None, "rotation": None, "breadth": None, "sicil": None,
                "summary": None, "summary_model": None, "built_at": datetime.now(config.LOCAL_TZ)}
    i = len(m.days) - 1
    day = m.days[i]
    r = regime.compute(m, i)
    stored = db.query_one("SELECT summary, summary_model, created_at FROM regime_history WHERE day = ? AND summary IS NOT NULL", (day,))
    if r and stored:
        summary, model = stored["summary"], stored["summary_model"]
    elif r:
        summary, model = regime.template_summary(r), "şablon (günlük özet henüz yazılmadı)"
    else:
        summary, model = None, None
    b = breadth.at(day)
    bh = breadth.history(m.days[max(0, i - 260)])
    sec = breadth.sector_breadth(day)
    prev = None
    if b:
        p = db.query_one("SELECT * FROM breadth_daily WHERE day < ? ORDER BY day DESC LIMIT 1", (b["day"],))
        prev = dict(p) if p else None
    const = breadth.constituents()
    return {
        "day": day, "regime": r, "summary": summary, "summary_model": model,
        "pulse": pulse.build(m, i), "rotation": rotation.report(m, i),
        "breadth": b, "breadth_prev": prev, "breadth_hist": bh, "sector_breadth": sec,
        "breadth_stale": bool(b and b["day"] != day), "constituents_stamp": const.fetched_at if const else None,
        "sicil": regime.track_record(20), "history": regime.history(m.days[max(0, i - 260)]),
        "built_at": datetime.now(config.LOCAL_TZ),
    }


def badge() -> dict | None:
    """Small regime chip for other pages (last stored snapshot; cheap)."""
    hit = _cache.get("badge")
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    r = db.query_one("SELECT day, overall, label, tone FROM regime_history ORDER BY day DESC LIMIT 1")
    val = dict(r) if r else None
    _cache["badge"] = (time.time(), val)
    return val


def rrg_plot(rows: list[dict], w: int = 560, h: int = 420, pad: int = 28) -> dict:
    """Scale RS-Ratio / RS-Momentum points (and 6-week tails) into SVG coordinates centred on 100/100."""
    pts = [(t["ratio"], t["mom"]) for r in rows for t in r["tail"]]
    if not pts:
        return {"w": w, "h": h, "items": []}
    dx = max(2.0, max(abs(x - 100) for x, _ in pts)) * 1.08
    dy = max(1.0, max(abs(y - 100) for _, y in pts)) * 1.08

    def sx(v):
        return pad + (v - (100 - dx)) / (2 * dx) * (w - 2 * pad)

    def sy(v):
        return h - pad - (v - (100 - dy)) / (2 * dy) * (h - 2 * pad)
    items = []
    for r in rows:
        tail = [(round(sx(t["ratio"]), 1), round(sy(t["mom"]), 1)) for t in r["tail"]]
        items.append({"symbol": r["symbol"], "quad": r["quad"], "group": r["group"], "x": tail[-1][0], "y": tail[-1][1],
                      "path": " ".join(f"{x},{y}" for x, y in tail)})
    return {"w": w, "h": h, "cx": round(sx(100), 1), "cy": round(sy(100), 1), "pad": pad, "items": items,
            "xmin": 100 - dx, "xmax": 100 + dx, "ymin": 100 - dy, "ymax": 100 + dy}
