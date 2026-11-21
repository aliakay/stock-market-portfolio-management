"""Vergi (German tax estimate) pages: /portfolio/vergi and /portfolio/vergi.csv."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter()


def _year(year) -> int:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return date.today().year
    return y if 1990 <= y <= date.today().year + 1 else date.today().year


@router.get("/portfolio/vergi", response_class=HTMLResponse)
def vergi_page(request: Request, year: str | None = None, msg: str | None = None):
    from .. import tax
    from .app import render
    return render(request, "vergi.html", r=tax.report(_year(year)), msg=msg)


@router.get("/portfolio/vergi.csv")
def vergi_csv(year: str | None = None):
    from .. import tax
    y = _year(year)
    body = "﻿" + tax.csv(y)          # BOM so Excel picks UTF-8
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="vergi_{y}.csv"'})


@router.post("/portfolio/vergi/fx-refresh")
def vergi_fx_refresh(year: str | None = None):
    from urllib.parse import quote

    from .. import tax
    res = tax.fx_refresh()
    tax.invalidate()
    m = (f"Kurlar güncellendi · {res.get('source')} · {res.get('rows', 0)} satır" if res.get("ok")
         else f"Kur güncellenemedi: {res.get('error') or 'bilinmeyen hata'}")
    return RedirectResponse(f"/portfolio/vergi?year={_year(year)}&msg={quote(m)}", status_code=303)
