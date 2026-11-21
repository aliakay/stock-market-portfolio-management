"""Price alerts page and form handlers."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import alerts

router = APIRouter()


def _back(back: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") else "/alarmlar"


@router.get("/alarmlar", response_class=HTMLResponse)
def alarms_page(request: Request, msg: str = "", err: str = ""):
    from .app import render
    return render(request, "alarmlar.html", rows=alerts.list_(include_inactive=True), msg=msg, err=err)


@router.post("/alarm/add")
def alarm_add(text: str = Form(...), note: str = Form(""), repeat: str = Form(""), back: str = Form("/alarmlar")):
    try:
        a = alerts.parse(text)
        alerts.add(a["symbol"], a["cond"], a["level"], note=note or None, repeat=bool(repeat))
    except ValueError as exc:
        return RedirectResponse(f"/alarmlar?err={quote(str(exc))}", status_code=303)
    return RedirectResponse(_back(back), status_code=303)


@router.post("/alarm/{aid}/delete")
def alarm_delete(aid: int, back: str = Form("/alarmlar")):
    alerts.remove(aid)
    return RedirectResponse(_back(back), status_code=303)
