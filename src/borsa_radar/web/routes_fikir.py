"""Fikir listesi sayfası: /fikirler (kural tabanlı hisse fikirleri + geçmiş ölçüm + canlı sicil), POST /fikirler/run."""

from __future__ import annotations

import logging
import threading
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger(__name__)
router = APIRouter()
_running = threading.Event()


@router.get("/fikirler", response_class=HTMLResponse)
def fikirler_page(request: Request, msg: str = ""):
    from .. import ideas
    from .app import render
    try:
        live = ideas.live_record()
    except Exception:  # noqa: BLE001
        log.exception("ideas live record failed")
        live = {}
    bt = ideas.backtest_result()
    return render(request, "fikirler.html", run=ideas.latest(), bt=bt, verdicts=ideas.verdicts(bt), live=live, screens=ideas.SCREENS,
                  running=_running.is_set(), msg=msg)


@router.post("/fikirler/run")
def fikirler_run():
    if not _running.is_set():
        _running.set()

        def job():
            try:
                from .. import ideas
                ideas.run()
            except Exception:  # noqa: BLE001
                log.exception("ideas run failed")
            finally:
                _running.clear()
        threading.Thread(target=job, daemon=True).start()
    return RedirectResponse("/fikirler?msg=" + quote("Tarama başladı; birkaç dakika sürer, sayfa kendini yenileyecek."), status_code=303)
