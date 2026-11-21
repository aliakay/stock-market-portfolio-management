"""/portfolio/davranis — trade behavior analytics (process coaching, not advice)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/portfolio/davranis", response_class=HTMLResponse)
def davranis_page(request: Request):
    from .. import behavior
    from .app import render
    return render(request, "davranis.html", r=behavior.compute())
