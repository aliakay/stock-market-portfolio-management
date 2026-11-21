"""Strateji laboratuvarı sayfası: /setups/strateji (geriye dönük test + stres testleri + bugünün sinyalleri + kağıt işlem defteri)."""

from __future__ import annotations

import logging
import threading
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger(__name__)
router = APIRouter()
_running = threading.Event()


def curve_path(points: list, w: float = 600, h: float = 140, lo: float | None = None, hi: float | None = None) -> str:
    """SVG polyline points for an equity curve [(yyyymmdd, value)], log scale."""
    import math
    if not points or len(points) < 2:
        return ""
    vals = [math.log(max(v, 1e-6)) for _, v in points]
    lo = math.log(lo) if lo else min(vals)
    hi = math.log(hi) if hi else max(vals)
    span = (hi - lo) or 1.0
    n = len(points) - 1
    return " ".join(f"{i / n * w:.1f},{h - (v - lo) / span * (h - 8) - 4:.1f}" for i, v in enumerate(vals))


def line_path(values: list, w: float = 600, h: float = 140, lo: float = 0.0, hi: float = 1.0) -> str:
    """SVG polyline points for plain values on a linear scale between lo and hi."""
    if not values or len(values) < 2:
        return ""
    span = (hi - lo) or 1.0
    n = len(values) - 1
    return " ".join(f"{i / n * w:.1f},{h - (v - lo) / span * (h - 8) - 4:.1f}" for i, v in enumerate(values))


@router.get("/setups/strateji", response_class=HTMLResponse)
def strateji_page(request: Request, msg: str = ""):
    from .. import lab_stress, strateji
    from .app import render
    res = strateji.results()
    return render(request, "strateji.html", res=res, rec=strateji.paper_record(), sig=strateji.signals(),
                  open_tr=strateji.open_trades(), closed_tr=strateji.closed_trades(), strategies=strateji.STRATEGIES,
                  slots=strateji.BOOK_SLOTS, running=_running.is_set(), msg=msg, curve_path=curve_path, line_path=line_path,
                  stress=lab_stress.latest(), journal=lab_stress.journal(), test_names=lab_stress.TEST_NAMES)


@router.post("/setups/strateji/run")
def strateji_run():
    if not _running.is_set():
        _running.set()

        def job():
            try:
                from .. import strateji
                strateji.daily_job()
            except Exception:  # noqa: BLE001
                log.exception("strateji run failed")
            finally:
                _running.clear()
        threading.Thread(target=job, daemon=True).start()
    return RedirectResponse("/setups/strateji?msg=" + quote("Sinyal taraması başladı; sayfa kendini yenileyecek."), status_code=303)
