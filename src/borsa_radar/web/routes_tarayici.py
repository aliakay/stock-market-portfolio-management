"""Hisse tarayıcı sayfası: /tarayici (filtreler + hazır taramalar + kayıtlı taramalar), CSV dışa aktarma, takibe alma."""

from __future__ import annotations

import logging
import threading
from datetime import date
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

log = logging.getLogger(__name__)
router = APIRouter()
_running = threading.Event()
SHOW = 150


def _params(request: Request) -> dict:
    return {k: v for k, v in request.query_params.items() if v not in (None, "") and k not in ("msg", "err")}


@router.get("/tarayici", response_class=HTMLResponse)
def tarayici_page(request: Request, msg: str = "", err: str = ""):
    from .. import screener
    from .app import render
    run, rows = screener.latest()
    raw = _params(request)
    flt, key, desc, preset = screener.resolve(raw)
    hits = screener.sort(screener.apply(rows, flt), key, desc)
    sectors = sorted({r["sector"] for r in rows if r.get("sector")})
    qs = urlencode(raw)
    active = {k: v for k, v in flt.items() if k not in ("sort", "dir")}
    return render(request, "tarayici.html", run=run, total=len(rows), hits=hits[:SHOW], n_hits=len(hits), show=SHOW,
                  flt=flt, raw=raw, active=active, sort_key=key, desc=desc, preset=preset, preset_code=raw.get("preset"),
                  presets=screener.PRESETS, fields=screener.FIELDS, field=screener.FIELD, groups=screener.GROUPS,
                  sectors=sectors, trend_tr=screener.TREND_TR, profit_tr=screener.PROFIT_TR, earn_tr=screener.EARN_TR,
                  screens=_screens(), saved=screener.saved(), qs=qs, running=_running.is_set(), msg=msg, err=err,
                  age=screener.age_days(run), today=date.today().isoformat(), sort_url=lambda k: _sort_url(raw, k, key, desc))


def _screens() -> dict:
    from .. import ideas
    return ideas.SCREENS


def _sort_url(raw: dict, k: str, cur: str, desc: bool) -> str:
    p = dict(raw)
    p["sort"] = k
    p["dir"] = "asc" if (k == cur and desc) else "desc"
    return "/tarayici?" + urlencode(p)


@router.get("/tarayici.csv")
def tarayici_csv(request: Request):
    from .. import screener
    _, rows = screener.latest()
    flt, key, desc, _ = screener.resolve(_params(request))
    body = screener.to_csv(screener.sort(screener.apply(rows, flt), key, desc))
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="tarama-{date.today().isoformat()}.csv"'})


@router.post("/tarayici/kaydet")
def tarayici_save(name: str = Form(...), query: str = Form("")):
    from .. import screener
    screener.save(name, query)
    return RedirectResponse(f"/tarayici?{query}&msg=" + quote(f"“{name.strip()[:60]}” kaydedildi"), status_code=303)


@router.post("/tarayici/sil/{sid}")
def tarayici_delete(sid: int):
    from .. import screener
    screener.delete(sid)
    return RedirectResponse("/tarayici?msg=" + quote("Kayıtlı tarama silindi"), status_code=303)


@router.post("/tarayici/izle")
def tarayici_watch(symbol: str = Form(...), back: str = Form("")):
    from .. import pipeline, tickers
    try:
        r = tickers.add(symbol)
        threading.Thread(target=pipeline.collect_stocks, daemon=True).start()
        note = "msg=" + quote(f"{r['symbol']} takibe alındı (haberler ve SEC dosyaları izlenecek)")
    except tickers.TickerError as exc:
        note = "err=" + quote(str(exc))
    return RedirectResponse(f"/tarayici?{back}&{note}", status_code=303)


@router.post("/tarayici/run")
def tarayici_run():
    if not _running.is_set():
        _running.set()

        def job():
            try:
                from .. import screener
                screener.run()
            except Exception:  # noqa: BLE001
                log.exception("screener run failed")
            finally:
                _running.clear()
        threading.Thread(target=job, daemon=True).start()
    return RedirectResponse("/tarayici?msg=" + quote("Tarama verisi yenileniyor; fiyatlar indirildiği için birkaç dakika sürer."), status_code=303)
