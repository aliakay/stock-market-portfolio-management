"""Karar sayfası: /karar (pano + tüm kartlar + sicil), POST /karar/run, /api/karar[/SEMBOL]."""

from __future__ import annotations

import logging
import threading
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger(__name__)
router = APIRouter()
_running = threading.Event()


def _board():
    try:
        from .. import karar
        return karar.board()
    except Exception:  # noqa: BLE001 - no engine / no run yet = "no verdict yet"
        return None


def _track():
    try:
        from .. import karar
        return karar.track_record(20)
    except Exception:  # noqa: BLE001
        return None


@router.get("/karar", response_class=HTMLResponse)
def karar_page(request: Request, msg: str = ""):
    from .app import render
    return render(request, "karar.html", board=_board(), tr=_track(), running=_running.is_set(), msg=msg)


@router.post("/karar/run")
def karar_run():
    if not _running.is_set():
        _running.set()

        def work():
            try:
                from .. import karar
                karar.run(use_llm=True)
            except Exception:  # noqa: BLE001
                log.exception("karar run failed")
            finally:
                _running.clear()
        threading.Thread(target=work, daemon=True).start()
    return RedirectResponse("/karar?msg=" + quote("Hesaplanıyor; sayfa kendini yenileyecek."), status_code=303)


@router.get("/api/karar")
def api_karar():
    return _board() or {"board": None}


@router.get("/api/karar/{symbol}")
def api_karar_symbol(symbol: str):
    try:
        from .. import karar
        c = karar.latest(symbol.strip().upper())
    except Exception:  # noqa: BLE001
        c = None
    if not c:
        raise HTTPException(404, f"{symbol.upper()}: karar kartı yok")
    return c
