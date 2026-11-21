"""Earnings pages: /h/{symbol}/bilanco (preview / post-mortem) and /bilancolar (upcoming list)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import earnings

router = APIRouter()


@router.get("/h/{symbol}/bilanco", response_class=HTMLResponse)
def bilanco_page(request: Request, symbol: str):
    from .app import render
    sym = symbol.upper().strip()
    return render(request, "bilanco.html", b=earnings.build(sym), earn=earnings)


@router.get("/h/{symbol}/bilanco.json")
def bilanco_json(symbol: str):
    return JSONResponse(earnings.brief(symbol.upper().strip()))


@router.get("/bilancolar", response_class=HTMLResponse)
def bilancolar_page(request: Request, days: int = 14):
    from .app import render
    days = max(1, min(days, 90))
    return render(request, "bilancolar.html", u=earnings.upcoming(days), earn=earnings)
