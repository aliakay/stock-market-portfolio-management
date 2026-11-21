"""İşlem öncesi kontrol sayfası: /portfolio/kontrol (form + sonuç aynı sayfada)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


def _num(s: str | None) -> float | None:
    """'1.234,5' / '1234.5' / '1234,5' → float; empty or invalid → None."""
    s = (s or "").strip().replace(" ", "").replace("$", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


@router.get("/portfolio/kontrol", response_class=HTMLResponse)
def kontrol_page(request: Request, side: str = "buy", symbol: str = "", qty: str = "", amount: str = "", price: str = "",
                 stop: str = ""):
    from .. import portfolio, precheck
    from .app import render
    form = {"side": side if side in ("buy", "sell") else "buy", "symbol": symbol.strip().upper(), "qty": qty, "amount": amount,
            "price": price, "stop": stop}
    r, err = None, ""
    if form["symbol"]:
        bad = [name for name, v in (("adet", qty), ("tutar", amount), ("fiyat", price), ("stop", stop)) if v.strip() and _num(v) is None]
        if bad:
            err = "Sayı okunamadı: " + ", ".join(bad)
        else:
            try:
                r = precheck.check(form["side"], form["symbol"], qty=_num(qty), amount=_num(amount), price=_num(price), stop=_num(stop))
            except ValueError as exc:
                err = str(exc)
    try:
        held = [p["symbol"] for p in portfolio.positions()]
    except Exception:  # noqa: BLE001
        held = []
    return render(request, "kontrol.html", r=r, form=form, err=err, held=held)
