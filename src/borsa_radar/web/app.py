"""Web UI (FastAPI + Jinja). Runs the scheduler in-process."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import analyzer, config, db, digest, hub, llm, market, market_report, notifier, pipeline, portfolio, prices, regime, scorecard, setups, technicals, tickers, research
from ..collectors import macro, kalshi, xbrl, positioning
from .. import importers
from .. import alerts as _alerts, thesis as _thesis
from ..util import ago, fmt_local, install_log_redaction, iso, us_market_open, utcnow

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    db.init()
    if not db.query_one("SELECT 1 FROM tickers"):
        tickers.seed_defaults(config.DEFAULT_TICKERS)
    if os.environ.get("BORSA_RADAR_NO_SCHEDULER") == "1":
        _scheduler = None
    else:
        _scheduler = pipeline.start_background()
    yield
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Borsa Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

DIR = {1: ("🟢", "pos"), 0: ("⚪", "neu"), -1: ("🔴", "neg"), None: ("·", "neu")}
TYPE_TR = {"news": "Haber", "rating": "Analist", "filing": "SEC", "macro": "Makro", "data": "Veri", "reg": "Düzenleme", "contract": "Devlet", "flow": "Pozisyon"}
templates.env.filters.update(
    local=fmt_local,
    ago=ago,
    dir_icon=lambda d: DIR.get(d, DIR[None])[0],
    dir_cls=lambda d: DIR.get(d, DIR[None])[1],
    type_tr=lambda t: TYPE_TR.get(t, t),
    loads=db.loads,
    pct=lambda v: "—" if v is None else f"{v:+.2f}%",
    money=lambda v: "—" if v is None else f"{v:,.2f}",
    usd=lambda v: "—" if v is None else (f"−${abs(v):,.0f}" if v < -0.5 else f"${v:,.0f}"),
    usd2=lambda v: "—" if v is None else (f"−${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"),
    susd=lambda v: "—" if v is None else (f"+${v:,.0f}" if v >= 0.5 else f"−${abs(v):,.0f}" if v <= -0.5 else "$0"),
    sgn=lambda v: "neu" if not isinstance(v, (int, float)) or abs(v) < 1e-9 else ("pos" if v > 0 else "neg"),
    qty=lambda v: "—" if v is None else (f"{v:,.0f}" if abs(v - round(v)) < 1e-6 else f"{v:,.4f}".rstrip("0").rstrip(".")),
    nyday=portfolio.ny_day,
    trdate=lambda d: "" if not d else f"{int(d[8:10])} {['Oca','Şub','Mar','Nis','May','Haz','Tem','Ağu','Eyl','Eki','Kas','Ara'][int(d[5:7]) - 1]} {d[:4]}",
)
templates.env.globals.update(market_open=us_market_open, base_url=config.PUBLIC_BASE_URL, tn=hub.num, tp=hub.pct, big=hub.big)
templates.env.globals.update(climate_label=regime.climate_label, weather_label=regime.weather_label, regime_badge=market_report.badge)
templates.env.filters["trnum"] = lambda t: re.sub(r"(?<=\d)\.(?=\d)", ",", t or "")


# One site: top-level sections, each with its own sub-tabs.
NAV = [
    ("Bugün", "/", [("Bugün", "/"), ("Karar", "/karar")]),
    ("Portföy", "/portfolio", [("Özet", "/portfolio"), ("Risk", "/portfolio/risk"), ("Tez defteri", "/tez"), ("İşlemler", "/portfolio/trades"),
                              ("Kapanan pozisyonlar", "/portfolio/closed"), ("Davranış", "/portfolio/davranis"),
                              ("İşlem öncesi kontrol", "/portfolio/kontrol"), ("Vergi", "/portfolio/vergi")]),
    ("Piyasa", "/piyasa", [("Rejim & Nabız", "/piyasa"), ("Makro gelişmeler", "/macro"), ("Takvim", "/calendar")]),
    ("Hisseler", "/tickers", [("Hisselerim", "/tickers"), ("Bilançolar", "/bilancolar"), ("Fiyat alarmları", "/alarmlar"), ("Fikir listesi", "/fikirler"), ("Tarayıcı", "/tarayici")]),
    ("Setup", "/setups", [("Günün setup'ları", "/setups"), ("Strateji laboratuvarı", "/setups/strateji")]),
    ("Akış", "/akis", [("Tüm gelişmeler", "/akis"), ("Günlük özetler", "/digests")]),
    ("Ayarlar", "/settings", [("Ayarlar", "/settings")]),
]


def _nav_state(path: str) -> tuple[str, list[tuple[str, str]], str]:
    """(active top section, its sub-tabs, active sub-tab href) for the current path."""
    if path.startswith(("/h/", "/s/")):
        sym = path.split("/")[2].upper()
        subs = [("Hisselerim", "/tickers"), (f"{sym} · Merkez", f"/h/{sym}"), (f"{sym} · Karne", f"/h/{sym}/karne"),
                (f"{sym} · Bilanço", f"/h/{sym}/bilanco")]
        return "Hisseler", subs, path.rstrip("/")
    if path.startswith("/e/"):
        return "Akış", NAV[5][2], "/akis"
    if path.startswith("/tez"):
        return "Portföy", NAV[1][2], "/tez"
    best = ("Bugün", NAV[0][2], "/")
    for title, href, subs in NAV:
        for _, shref in subs:
            if shref != "/" and (path == shref or path.startswith(shref + "/")):
                if len(shref) > len(best[2]) or best[2] == "/":
                    best = (title, subs, shref)
    return best


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("nav_tickers", db.active_tickers())
    sec, subs, sub_active = _nav_state(request.url.path)
    ctx.update(nav=NAV, nav_section=sec, nav_subs=subs, nav_sub_active=sub_active)
    return templates.TemplateResponse(request, name, ctx)


def _events(*, symbol=None, types=None, min_imp=0, days=7, limit=150, include_backfill=True, include_irrelevant=False):
    where = ["e.published_at >= ?"]
    params: list = [iso(utcnow() - timedelta(days=days))]
    statuses = ["analyzed", "pending"] + (["backfill"] if include_backfill else []) + (["irrelevant"] if include_irrelevant else [])
    where.append(f"e.status IN ({','.join('?' * len(statuses))})")
    params += statuses
    if symbol:
        where.append("EXISTS (SELECT 1 FROM event_symbols s WHERE s.event_id = e.id AND s.symbol = ?)")
        params.append(symbol)
    if types:
        where.append(f"e.type IN ({','.join('?' * len(types))})")
        params += list(types)
    if min_imp:
        where.append("COALESCE(e.importance, 0) >= ?")
        params.append(min_imp)
    rows = db.query(
        f"""SELECT e.*, a.summary_tr, a.reasoning_tr, a.category, a.portfolio_impact,
                   (SELECT group_concat(symbol, ' ') FROM event_symbols s WHERE s.event_id = e.id) AS syms
            FROM events e LEFT JOIN analyses a ON a.event_id = e.id
            WHERE {' AND '.join(where)} ORDER BY e.published_at DESC LIMIT ?""",
        params + [limit],
    )
    return rows


# ── pages ─────────────────────────────────────────────────

def _attention() -> dict:
    """What needs a look today: triggered thesis rules, reviews due, positions without a thesis, risk warnings, fired alerts."""
    out: dict = {"theses": [], "missing": [], "risk": [], "alerts": []}
    try:
        ov = _thesis.overview()
        out["theses"], out["missing"] = ov["attention"], ov["missing"]
    except Exception:  # noqa: BLE001
        log.exception("attention: thesis")
    try:
        from .. import risk
        r = risk.compute()
        out["risk"] = [w for w in (r or {}).get("warnings", []) if w["level"] == "warn"][:3]
    except Exception:  # noqa: BLE001
        log.exception("attention: risk")
    since = (utcnow() - timedelta(hours=36)).isoformat()
    out["alerts"] = [a for a in _alerts.list_(include_inactive=True) if a["fired_at"] and a["fired_at"] >= since]
    return out


def _karar_board() -> dict | None:
    """Last stored Karar board, or None (engine missing / nothing computed yet): pages must render without it."""
    try:
        from .. import karar
        return karar.board()
    except Exception:  # noqa: BLE001
        return None


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Bugün: one screen that ties the portfolio, market regime, setups, alerts and calendar together."""
    led = portfolio.build()
    perf = portfolio.total_perf(led) if led.days else None
    pos = portfolio.positions(led=led)
    since = iso(utcnow() - timedelta(days=3))
    for p in pos:
        c = scorecard.latest(p["symbol"])
        p["karne"] = c and {"overall": c["overall"], "tone": c["tone"], "label": c["label"]}
        e = db.query_one("""SELECT e.id, e.title, e.published_at, COALESCE(a.direction, e.direction) d FROM events e
                            JOIN event_symbols s ON s.event_id = e.id LEFT JOIN analyses a ON a.event_id = e.id
                            WHERE s.symbol = ? AND e.published_at >= ? AND COALESCE(e.importance, 0) >= 3
                              AND e.status IN ('analyzed','pending') ORDER BY e.published_at DESC LIMIT 1""", (p["symbol"], since))
        p["event"] = dict(e) if e else None
    open_s = setups.open_setups()
    for p in pos:
        p["setup"] = next((x for x in open_s if x["symbol"] == p["symbol"]), None)
    held = {p["symbol"] for p in pos}
    watch = [t["symbol"] for t in db.active_tickers() if t["symbol"] not in held]
    rep = setups.report()
    top = _events(min_imp=4, days=1, limit=12, include_backfill=False)
    digest_row = db.query_one("SELECT kind, day, content, created_at FROM digests ORDER BY created_at DESC LIMIT 1")
    reg = regime.latest()
    attention = _attention()
    board = _karar_board()
    kcards = {c["symbol"]: c for c in (board or {}).get("cards") or []}
    if board:
        # the board already covers thesis rules and missing theses for the stocks it has a card for: say it once
        attention["theses"] = [t for t in attention["theses"] if t["symbol"] not in kcards]
        attention["missing"] = [s for s in attention["missing"] if s not in kcards]
        order = {s: i for i, s in enumerate(kcards)}
        pos.sort(key=lambda p: order.get(p["symbol"], len(order)))  # stable: positions without a card keep their order, last
    return render(request, "bugun.html", board=board, kcards=kcards, perf=perf, headline=portfolio.headline(perf) if perf else None, pos=pos,
                  reg=reg, setups_rep=rep, open_setups=open_s, top=top, upcoming=macro.upcoming(48),
                  watch_quotes=market.quotes(watch) if watch else {}, digest=dict(digest_row) if digest_row else None,
                  updated=prices.last_update(), attention=attention)


@app.get("/akis", response_class=HTMLResponse)
def feed(request: Request, symbol: str = "", type: str = "", imp: int = 3, days: int = 3, all: int = 0):
    types = [type] if type else None
    rows = _events(symbol=symbol.upper() or None, types=types, min_imp=imp if not all else 0, days=days,
                   include_backfill=bool(all) or imp <= 1, include_irrelevant=bool(all))
    quotes = market.quotes([t["symbol"] for t in db.active_tickers()])
    upcoming = macro.upcoming(48)
    return render(request, "feed.html", rows=rows, quotes=quotes, upcoming=upcoming,
                  f={"symbol": symbol.upper(), "type": type, "imp": imp, "days": days, "all": all})


@app.get("/e/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: int):
    e = db.get_event(event_id)
    if not e:
        raise HTTPException(404)
    reactions_ = db.query("SELECT * FROM price_reactions WHERE event_id = ? ORDER BY symbol", (event_id,))
    return render(request, "event.html", e=e, syms=db.event_symbols(event_id), extra=db.loads(e["extra"], {}), research=research.get(event_id),
                  impact=db.loads(e["portfolio_impact"], {}), reactions=reactions_, instruments=config.MARKET_INSTRUMENTS)


@app.post("/e/{event_id}/analyze")
def event_analyze(event_id: int):
    threading.Thread(target=analyzer.analyze_one, args=(event_id,), daemon=True).start()
    return RedirectResponse(f"/e/{event_id}?analyzing=1", status_code=303)


@app.post("/e/{event_id}/research")
def event_research(event_id: int):
    research.request(event_id)
    return RedirectResponse(f"/e/{event_id}", status_code=303)


@app.get("/s/{symbol}")
def stock_page(symbol: str, imp: int = 2):
    """Old stock page: merged into the stock hub."""
    return RedirectResponse(f"/h/{quote(symbol.upper())}?imp={imp}#analizler", status_code=301)


@app.get("/macro", response_class=HTMLResponse)
def macro_page(request: Request, imp: int = 3, days: int = 7):
    rows = _events(types=["macro", "data", "reg"], min_imp=imp, days=days, limit=150, include_backfill=imp <= 1)
    quotes = market.quotes(list(config.MARKET_INSTRUMENTS))
    cal = macro.upcoming(24 * 14)
    return render(request, "macro.html", rows=rows, quotes=quotes, instruments=config.MARKET_INSTRUMENTS, cal=cal, imp=imp, days=days, kalshi=kalshi.overview())


@app.get("/api/positioning/{symbol}")
def api_positioning(symbol: str):
    symbol = symbol.upper()
    return {"short_volume": positioning.short_volume_series(symbol, 60),
            "short_interest": positioning.short_interest_series(symbol, 12),
            "social": positioning.social_series(symbol, 30)}


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    cal = db.query(
        "SELECT * FROM macro_calendar WHERE release_at >= ? ORDER BY release_at LIMIT 200",
        (iso(utcnow() - timedelta(days=7)),),
    )
    return render(request, "calendar.html", cal=cal, now=iso(utcnow()))


@app.get("/digests", response_class=HTMLResponse)
def digests_page(request: Request):
    rows = db.query("SELECT * FROM digests ORDER BY created_at DESC LIMIT 60")
    return render(request, "digests.html", rows=rows)


@app.post("/digests/send")
def digests_send(kind: str = Form(...)):
    threading.Thread(target=digest.send, args=(kind,), kwargs={"force": True}, daemon=True).start()
    return RedirectResponse("/digests?sending=1", status_code=303)


@app.get("/tickers", response_class=HTMLResponse)
def tickers_page(request: Request, msg: str = "", err: str = ""):
    rows = [dict(r) for r in db.all_tickers()]
    pos = {p["symbol"]: p for p in portfolio.positions() if p["asset"] == "stock"}
    open_s = setups.open_setups()
    for r in rows:
        r["pos"] = pos.get(r["symbol"])
        c = scorecard.latest(r["symbol"])
        r["karne"] = c and {"overall": c["overall"], "tone": c["tone"]}
        r["setup"] = next((x for x in open_s if x["symbol"] == r["symbol"]), None)
    unwatched = [p for s_, p in pos.items() if not db.get_ticker(s_)]
    rows.sort(key=lambda r: (not r["active"], r["pos"] is None, -(r["pos"]["value"] if r["pos"] else 0), r["symbol"]))
    quotes = market.quotes([r["symbol"] for r in rows if r["active"]])
    return render(request, "tickers.html", rows=rows, quotes=quotes, msg=msg, err=err, unwatched=unwatched,
                  default_threshold=db.get_setting("threshold_stock"))


@app.post("/tickers/add")
def tickers_add(symbols: str = Form(...), notes: str = Form("")):
    added, errors = [], []
    for s in symbols.replace(",", " ").split():
        try:
            r = tickers.add(s, notes=notes or None)
            added.append(r["symbol"])
        except tickers.TickerError as exc:
            errors.append(str(exc))
    if added:
        threading.Thread(target=pipeline.collect_stocks, daemon=True).start()
    return RedirectResponse(f"/tickers?msg={' '.join(added) + ' eklendi' if added else ''}&err={'; '.join(errors)}", status_code=303)


@app.post("/tickers/{symbol}/toggle")
def tickers_toggle(symbol: str):
    t = db.get_ticker(symbol)
    if t:
        db.execute("UPDATE tickers SET active = ? WHERE symbol = ?", (0 if t["active"] else 1, symbol.upper()))
    return RedirectResponse("/tickers", status_code=303)


@app.post("/tickers/{symbol}/delete")
def tickers_delete(symbol: str):
    try:
        tickers.remove(symbol, purge=True)
    except tickers.TickerError:
        pass
    return RedirectResponse(f"/tickers?msg={symbol.upper()} silindi", status_code=303)


@app.post("/tickers/{symbol}/threshold")
def tickers_threshold(symbol: str, threshold: str = Form("")):
    try:
        if threshold.strip():
            tickers.set_options(symbol, threshold=int(threshold))
        else:
            tickers.set_options(symbol, clear_threshold=True)
    except (tickers.TickerError, ValueError) as exc:
        return RedirectResponse(f"/tickers?err={exc}", status_code=303)
    return RedirectResponse("/tickers", status_code=303)


SETTING_LABELS = {
    "quiet_start": "Sessiz saat başlangıcı (HH:MM)",
    "quiet_end": "Sessiz saat bitişi (HH:MM)",
    "digest_morning": "Sabah özeti (HH:MM)",
    "digest_evening": "Akşam özeti (HH:MM)",
    "threshold_stock": "Hisse haberi anlık bildirim eşiği (1-5; yalnız izlenen, portföyde olmayan hisselerde +1)",
    "threshold_macro": "Makro bildirim eşiği (1-5)",
    "rating_pt_change_pct": "Hedef fiyat değişimi 'önemli' sayılsın (%)",
    "rate_limit_per_10min": "10 dakikada en fazla mesaj",
    "telegram_enabled": "Telegram bildirimleri (1/0)",
    "llm_provider": "LLM sağlayıcı", "llm_model": "LLM model",
    "llm_fallback_provider": "Yedek sağlayıcı", "llm_fallback_model": "Yedek model",
    "llm_daily_cap": "Günlük LLM çağrı limiti",
    "watch_holdings": "Portföydeki hisseleri otomatik izle (1/0)",
    "thesis_alerts": "Tez kuralı tetiklenince Telegram (1/0)",
    "price_alerts": "Fiyat alarmı Telegram bildirimi (1/0)",
    "weekly_digest": "Pazar 18:00 'Haftaya bakış' özeti (1/0)",
    "risk_per_trade_pct": "İşlem öncesi kontrol: işlem başına risk (% portföy)",
    "max_position_pct": "İşlem öncesi kontrol: tek pozisyon üst sınırı (%)",
    "setup_extra": "Setup tarama havuzu (portföy + izleme listesine ek, boşlukla ayır)",
    "setup_wide": "Setup keşif evreni: sp500 sp400 sp600 (boşlukla ayır; boş = kapalı)",
    "setup_alerts": "Setup Telegram özeti (1 açık / 0 kapalı)",
}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    jobs = db.query("SELECT * FROM job_runs ORDER BY job")
    counts = db.query("SELECT type, status, COUNT(*) n FROM events GROUP BY type, status ORDER BY type, status")
    notif = db.query("SELECT * FROM notifications WHERE status != 'dropped' ORDER BY created_at DESC LIMIT 15")
    return render(request, "settings.html", s=db.all_settings(), labels=SETTING_LABELS, saved=saved, jobs=jobs,
                  counts=counts, notif=notif, llm_today=llm.usage_today(), disk=pipeline.disk_free_gb())


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for k in SETTING_LABELS:
        if k in form:
            db.set_setting(k, str(form[k]).strip())
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test-telegram")
def settings_test_telegram():
    ok = notifier.send_now("✅ <b>Borsa Radar</b> test mesajı.")
    return RedirectResponse(f"/settings?saved={'2' if ok else '3'}", status_code=303)


@app.get("/reports/{symbol}/{name}")
def report_file(symbol: str, name: str):
    path = (config.REPORTS_DIR / symbol.upper() / name).resolve()
    if not str(path).startswith(str(config.REPORTS_DIR.resolve())) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=name)


# ── portfolio ─────────────────────────────────────────────

def _local_ts(value: str) -> str | None:
    """<input type=datetime-local> value (Berlin wall clock) → UTC ISO; '' → now."""
    value = (value or "").strip()
    if not value:
        return None
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=config.LOCAL_TZ)
    return iso(d)


def _refresh_async(symbols: list[str] | None = None) -> None:
    def run():
        try:
            prices.refresh(symbols) if symbols else portfolio.refresh_prices()
        finally:
            portfolio.invalidate()
    threading.Thread(target=run, daemon=True).start()


def _back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}msg={quote(msg)}&err={quote(err)}", status_code=303)


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request, msg: str = "", err: str = ""):
    led = portfolio.build()
    perf = portfolio.total_perf(led)
    alloc = portfolio.allocation(led)
    pos = portfolio.positions(led=led)
    closed = portfolio.closed_positions(led)
    hold = [c["days"] for c in closed]
    return render(request, "portfolio.html", perf=perf, alloc=alloc, pos=pos, periods=portfolio.PERIODS,
                  headline=portfolio.headline(perf), recent=portfolio.recent_summary(7), warnings=led.warnings,
                  n_closed=len(closed), avg_hold=sum(hold) / len(hold) if hold else None,
                  n_option=sum(1 for p in pos if p["asset"] == "option"), updated=prices.last_update(),
                  subs=portfolio.subportfolios(), msg=msg, err=err)


@app.get("/api/portfolio/chart")
def api_portfolio_chart(range: str = "ytd", sub: int = 0):
    return portfolio.chart(range, sub or None)


@app.post("/portfolio/refresh")
def portfolio_refresh():
    _refresh_async()
    return _back("/portfolio", "Fiyatlar arka planda güncelleniyor; birkaç saniye sonra sayfayı yenile.")


@app.get("/portfolio/trades", response_class=HTMLResponse)
def trades_page(request: Request, asset: str = "", symbol: str = "", sub: int = 0, page: int = 1,
                msg: str = "", err: str = ""):
    per = 50
    page = max(page, 1)
    rows, n = portfolio.trades(asset=asset or None, symbol=symbol or None, sub_id=sub or None,
                               limit=per, offset=(page - 1) * per)
    return render(request, "trades.html", rows=rows, n=n, page=page, pages=max((n + per - 1) // per, 1),
                  f={"asset": asset, "symbol": symbol.upper(), "sub": sub}, stats=portfolio.month_stats(),
                  subs=portfolio.subportfolios(), cash=portfolio.cash_flows(30), kinds=portfolio.KIND_TR,
                  msg=msg, err=err, result=None)


@app.post("/portfolio/trades/add")
def trades_add(side: str = Form(...), symbol: str = Form(...), qty: float = Form(...), price: float = Form(...),
               fees: float = Form(0), sub: str = Form(...), when: str = Form(""), note: str = Form(""),
               currency: str = Form("USD"), asset: str = Form("")):
    try:
        tid = portfolio.add_trade(side=side, symbol=symbol, qty=qty, price=price, fees=fees, sub=sub,
                                  ts=_local_ts(when), note=note, currency=currency or "USD", asset=asset or None)
    except (portfolio.LedgerError, ValueError) as exc:
        return _back("/portfolio/trades", err=str(exc))
    _refresh_async([symbol.upper(), *portfolio.price_symbols()])
    return _back("/portfolio/trades", f"İşlem #{tid} eklendi.")


@app.post("/portfolio/trades/{trade_id}/edit")
def trades_edit(trade_id: int, sub: str = Form(""), note: str = Form("")):
    try:
        portfolio.update_trade(trade_id, sub=sub or None, note=note)
    except portfolio.LedgerError as exc:
        return _back("/portfolio/trades", err=str(exc))
    return _back("/portfolio/trades", f"İşlem #{trade_id} güncellendi.")


@app.post("/portfolio/trades/{trade_id}/delete")
def trades_delete(trade_id: int):
    portfolio.delete_trade(trade_id)
    return _back("/portfolio/trades", f"İşlem #{trade_id} silindi.")


@app.post("/portfolio/trades/move")
def trades_move(symbol: str = Form(...), sub: str = Form(...)):
    try:
        n = portfolio.move_symbol(symbol, sub)
    except portfolio.LedgerError as exc:
        return _back("/portfolio/trades", err=str(exc))
    return _back("/portfolio/trades", f"{symbol.upper()}: {n} işlem taşındı.")


@app.post("/portfolio/cash/add")
def cash_add(kind: str = Form(...), amount: float = Form(...), sub: str = Form(...), to_sub: str = Form(""),
             when: str = Form(""), note: str = Form(""), currency: str = Form("USD")):
    try:
        if kind == "transfer":
            portfolio.transfer(amount, sub, to_sub, ts=_local_ts(when), note=note or None)
        else:
            portfolio.add_cash(kind=kind, amount=amount, sub=sub, ts=_local_ts(when), note=note, currency=currency or "USD")
    except (portfolio.LedgerError, ValueError) as exc:
        return _back("/portfolio/trades", err=str(exc))
    return _back("/portfolio/trades", "Nakit hareketi kaydedildi.")


@app.post("/portfolio/cash/{cash_id}/delete")
def cash_delete(cash_id: int):
    portfolio.delete_cash(cash_id)
    return _back("/portfolio/trades", "Nakit hareketi silindi.")


@app.post("/portfolio/import", response_class=HTMLResponse)
async def portfolio_import(request: Request, file: UploadFile = File(...), sub: str = Form(...), dry_run: str = Form("")):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        res = importers.import_text(text, sub, dry_run=bool(dry_run))
    except portfolio.LedgerError as exc:
        return _back("/portfolio/trades", err=str(exc))
    if not dry_run and (res["trades"] or res["cash"]):
        _refresh_async()

        def _watch():
            if tickers.sync_holdings():
                pipeline.collect_stocks()
        threading.Thread(target=_watch, daemon=True).start()
    rows, n = portfolio.trades(limit=50)
    return render(request, "trades.html", rows=rows, n=n, page=1, pages=max((n + 49) // 50, 1),
                  f={"asset": "", "symbol": "", "sub": 0}, stats=portfolio.month_stats(), subs=portfolio.subportfolios(),
                  cash=portfolio.cash_flows(30), kinds=portfolio.KIND_TR, msg="", err="",
                  result=res | {"dry_run": bool(dry_run), "filename": file.filename})


@app.post("/portfolio/subs/add")
def subs_add(code: str = Form(...), name: str = Form(...), color: str = Form("")):
    try:
        portfolio.add_sub(code, name, color or None)
    except portfolio.LedgerError as exc:
        return _back("/portfolio/trades", err=str(exc))
    return _back("/portfolio/trades", f"{code.upper()} eklendi.")


@app.post("/portfolio/subs/{sub_id}/edit")
def subs_edit(sub_id: int, name: str = Form(...), color: str = Form("")):
    try:
        portfolio.update_sub(sub_id, name=name, color=color or None)
    except portfolio.LedgerError as exc:
        return _back("/portfolio/trades", err=str(exc))
    return _back("/portfolio/trades", "Alt portföy güncellendi.")


# ── thesis notebook ──────────────────────────────────────

def _num_or_none(v: str):
    v = (v or "").strip().replace(",", ".")
    return float(v) if v else None


@app.get("/tez", response_class=HTMLResponse)
def tez_page(request: Request, symbol: str = "", msg: str = "", err: str = ""):
    from .. import thesis
    ov = thesis.overview()
    return render(request, "tez.html", ov=ov, new_symbol=symbol.upper(), kinds=thesis.KINDS, rule_kinds=thesis.RULES,
                  msg=msg, err=err)


@app.post("/tez/add")
def tez_add(symbol: str = Form(...), text: str = Form(...), kind: str = Form(""), catalysts: str = Form(""),
            target: str = Form(""), conviction: str = Form(""), review_days: int = Form(30), rules: str = Form(""),
            status: str = Form("active")):
    from .. import thesis
    try:
        tid = thesis.add(symbol, text, kind=kind or None, catalysts=catalysts or None, target=_num_or_none(target),
                         conviction=int(conviction) if conviction else None, review_days=review_days,
                         rules=[r for r in rules.splitlines() if r.strip()], status=status)
    except ValueError as exc:
        return RedirectResponse(f"/tez?symbol={quote(symbol)}&err={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/tez?msg={quote(symbol.upper() + ' tezi kaydedildi')}#t{tid}", status_code=303)


@app.post("/tez/{tid}/rule")
def tez_rule(tid: int, rule: str = Form(...)):
    from .. import thesis
    try:
        k, p, n = thesis.parse_rule(rule)
        thesis.add_rule(tid, k, p, n)
        thesis.check(tid)
    except ValueError as exc:
        return RedirectResponse(f"/tez?err={quote('Kural anlaşılamadı: ' + str(exc))}#t{tid}", status_code=303)
    return RedirectResponse(f"/tez#t{tid}", status_code=303)


@app.post("/tez/rule/{rid}/delete")
def tez_rule_delete(rid: int, tid: int = Form(...)):
    from .. import thesis
    thesis.delete_rule(rid)
    return RedirectResponse(f"/tez#t{tid}", status_code=303)


@app.post("/tez/{tid}/note")
def tez_note(tid: int, text: str = Form(...)):
    from .. import thesis
    thesis.note(tid, text)
    return RedirectResponse(f"/tez#t{tid}", status_code=303)


@app.post("/tez/{tid}/review")
def tez_review(tid: int, text: str = Form(""), keep: str = Form("1")):
    from .. import thesis
    thesis.reviewed(tid, text or None, keep=keep == "1")
    return RedirectResponse(f"/tez#t{tid}", status_code=303)


@app.post("/tez/{tid}/edit")
def tez_edit(tid: int, text: str = Form(...), kind: str = Form(""), catalysts: str = Form(""), conviction: str = Form(""),
             review_days: int = Form(30), status: str = Form("")):
    from .. import thesis
    thesis.update(tid, thesis=text, kind=kind or None, catalysts=catalysts, conviction=int(conviction) if conviction else None,
                  review_days=review_days, status=status or None)
    return RedirectResponse(f"/tez#t{tid}", status_code=303)


@app.post("/tez/{tid}/close")
def tez_close(tid: int, lessons: str = Form("")):
    from .. import thesis
    thesis.close(tid, lessons or None)
    return RedirectResponse(f"/tez#t{tid}", status_code=303)


@app.post("/tez/{tid}/delete")
def tez_delete(tid: int):
    from .. import thesis
    thesis.delete(tid)
    return RedirectResponse("/tez?msg=Tez%20silindi", status_code=303)


@app.post("/tez/check")
def tez_check():
    from .. import thesis
    n = thesis.check()
    return RedirectResponse(f"/tez?msg={quote(f'Kurallar kontrol edildi · {len(n)} yeni tetik')}", status_code=303)


@app.get("/portfolio/risk", response_class=HTMLResponse)
def risk_page(request: Request):
    from .. import risk
    return render(request, "risk.html", r=risk.compute())


@app.get("/portfolio/closed", response_class=HTMLResponse)
def closed_page(request: Request):
    rows = portfolio.closed_positions()
    wins = [r for r in rows if r["realized"] > 0]
    losses = [r for r in rows if r["realized"] <= 0]
    summary = {
        "n": len(rows), "wins": len(wins), "losses": len(losses),
        "hit": len(wins) / len(rows) * 100 if rows else None,
        "realized": sum(r["realized"] for r in rows),
        "avg_win": sum(r["pct"] or 0 for r in wins) / len(wins) if wins else None,
        "avg_loss": sum(r["pct"] or 0 for r in losses) / len(losses) if losses else None,
        "avg_days": sum(r["days"] for r in rows) / len(rows) if rows else None,
    }
    return render(request, "closed.html", rows=rows, s=summary)


# ── JSON API ──────────────────────────────────────────────

@app.get("/api/chart/{symbol}")
def api_chart(symbol: str, range: str = "3mo"):
    symbol = symbol.upper()
    try:
        candles = market.chart_series(symbol, range)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    start = candles[0]["time"] if candles else 0
    markers = []
    rows = db.query(
        """SELECT e.id, e.type, e.title, e.published_at, e.importance, e.direction FROM events e
           JOIN event_symbols s ON s.event_id = e.id
           WHERE s.symbol = ? AND e.status IN ('analyzed','backfill') AND
                 ((e.type = 'rating' AND COALESCE(e.importance,0) >= 3) OR (e.type != 'rating' AND COALESCE(e.importance,0) >= 3))
           ORDER BY e.published_at""",
        (symbol,),
    )
    from ..util import parse_dt
    for r in rows:
        ts = int(parse_dt(r["published_at"]).timestamp())
        if ts < start:
            continue
        markers.append({"time": ts, "id": r["id"], "type": r["type"], "title": r["title"],
                        "direction": r["direction"], "importance": r["importance"]})
    return {"candles": candles, "markers": markers}


@app.get("/api/context")
def api_context(symbol: str = ""):
    """Same JSON as `borsa-radar context --json` (Hermes / scripts)."""
    from .. import context as ctx
    return JSONResponse(json.loads(ctx.dumps(ctx.stock(symbol) if symbol else ctx.snapshot())))


@app.get("/api/events")
def api_events(symbol: str = "", days: int = 3, imp: int = 3):
    rows = _events(symbol=symbol.upper() or None, min_imp=imp, days=days, include_backfill=False)
    return [dict(r) for r in rows]


# ── market: regime + pulse + rotation ────────────────────

@app.get("/piyasa", response_class=HTMLResponse)
def piyasa_page(request: Request, msg: str = ""):
    rep = market_report.build()
    rrg = market_report.rrg_plot(rep["rotation"]["rows"]) if rep.get("rotation") else {"items": []}
    return render(request, "piyasa.html", rep=rep, rrg=rrg, msg=msg)


@app.post("/piyasa/yenile")
def piyasa_refresh():
    market_report.build(force=True)
    return RedirectResponse("/piyasa?msg=" + quote("Yeniden hesaplandı (fiyatlar 15 dakikada bir güncellenir)."), status_code=303)


# ── stock hub + scorecard ─────────────────────────────────

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
_karne_running: set[str] = set()


def _sym(symbol: str) -> str:
    s = symbol.strip().upper().replace("/", "-")
    if not SYMBOL_RE.match(s):
        raise HTTPException(404, "geçersiz sembol")
    return s


@app.get("/h")
def hub_search(s: str = ""):
    s = s.strip().upper()
    return RedirectResponse(f"/h/{quote(s)}" if s else "/portfolio", status_code=303)


@app.get("/h/{symbol}", response_class=HTMLResponse)
def hub_page(request: Request, symbol: str, imp: int = 2):
    sym = _sym(symbol)
    h = hub.build(sym)
    if not h["tech"] and not h["quote"]:
        raise HTTPException(404, f"{sym}: fiyat verisi bulunamadı")
    active = [x for x in setups.open_setups() if x["symbol"] == sym]
    rows = _events(symbol=sym, types=["news", "filing", "data", "macro", "rating", "reg", "contract", "flow"], min_imp=imp, days=30,
                   limit=80, include_backfill=imp <= 1)
    rdir = config.REPORTS_DIR / sym
    reports = sorted(rdir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if rdir.exists() else []
    try:
        from .. import karar
        kc = karar.current(sym)   # latest run only: no stale card after the position is sold
    except Exception:  # noqa: BLE001
        kc = None
    return render(request, "hub.html", h=h, kc=kc, card=scorecard.latest(sym), active_setups=active, ev_rows=rows, imp=imp,
                  reports=reports, fin=xbrl.quarter_table(sym, 8) if h["watched"] else [], fmt_usd=xbrl._fmt_usd,
                  upcoming=[c for c in macro.upcoming(24 * 90) if c["key"] == f"earnings:{sym}"],
                  tez=_thesis.for_symbol(sym), price_alerts=_alerts.list_(sym),
                  position=next((p for p in portfolio.positions() if p["symbol"] == sym), None))


@app.get("/api/hub/{symbol}/bars")
def hub_bars(symbol: str, range: str = "6mo"):  # noqa: A002
    sym = _sym(symbol)
    bs = technicals.ensure_bars(sym)
    days = {"3mo": 63, "6mo": 126, "1y": 252, "2y": 504}.get(range, 126)
    closes = [b["close"] for b in bs]

    def ma(n):
        out, acc = [], 0.0
        for i, c in enumerate(closes):
            acc += c
            if i >= n:
                acc -= closes[i - n]
            if i >= n - 1:
                out.append({"time": bs[i]["day"], "value": round(acc / n, 4)})
        return out[-days:]
    bars = [{"time": b["day"], "open": b["open"] or b["close"], "high": b["high"] or b["close"], "low": b["low"] or b["close"],
             "close": b["close"]} for b in bs[-days:]]
    markers = []
    if bars:
        from ..util import parse_dt
        days_ = [b["time"] for b in bars]
        per_day: dict[str, dict] = {}
        for r in db.query("""SELECT e.id, e.type, e.published_at, COALESCE(a.importance, e.importance) imp,
                                    COALESCE(a.direction, e.direction) d FROM events e JOIN event_symbols s ON s.event_id = e.id
                             LEFT JOIN analyses a ON a.event_id = e.id
                             WHERE s.symbol = ? AND e.published_at >= ? AND e.status IN ('analyzed','backfill')
                               AND COALESCE(a.importance, e.importance, 0) >= 3 ORDER BY e.published_at""", (sym, days_[0])):
            day = parse_dt(r["published_at"]).astimezone(config.NY_TZ).date().isoformat()
            t = next((d for d in days_ if d >= day), None)
            if not t:
                continue
            m = per_day.setdefault(t, {"time": t, "n": 0, "pos": 0, "neg": 0, "best": None})
            m["n"] += 1
            m["pos"] += (r["d"] or 0) > 0
            m["neg"] += (r["d"] or 0) < 0
            if m["best"] is None or (r["imp"] or 0) > m["best"]["imp"]:
                m["best"] = {"id": r["id"], "type": r["type"], "imp": r["imp"] or 0}
        # one marker per trading day: net direction, the top event is the click target, text = count
        for t, m in sorted(per_day.items()):
            net = m["pos"] - m["neg"]
            markers.append({"time": t, "id": m["best"]["id"], "type": m["best"]["type"], "importance": m["best"]["imp"],
                            "direction": 1 if net > 0 else -1 if net < 0 else 0, "count": m["n"]})
    return {"bars": bars, "sma50": ma(50), "sma200": ma(200), "markers": markers}


@app.get("/h/{symbol}/karne", response_class=HTMLResponse)
def karne_page(request: Request, symbol: str, msg: str = ""):
    sym = _sym(symbol)
    return render(request, "karne.html", sym=sym, card=scorecard.latest(sym), hist=scorecard.history(sym, 30),
                  tr=scorecard.track_record(20), running=sym in _karne_running, msg=msg)


@app.post("/h/{symbol}/karne/yeni")
def karne_new(symbol: str):
    sym = _sym(symbol)
    if sym not in _karne_running:
        _karne_running.add(sym)

        def work():
            try:
                scorecard.run(sym, use_llm=True)
            except Exception:  # noqa: BLE001
                log.exception("scorecard %s failed", sym)
            finally:
                _karne_running.discard(sym)
        threading.Thread(target=work, daemon=True).start()
    return RedirectResponse(f"/h/{sym}/karne?msg=" + quote("Hesaplanıyor; sayfa kendini yenileyecek."), status_code=303)


# ── setup scanner ─────────────────────────────────────────

_setups_running = threading.Event()


@app.get("/setups", response_class=HTMLResponse)
def setups_page(request: Request, day: str | None = None, msg: str = ""):
    return render(request, "setups.html", r=setups.report(day), rules_ver=setups.RULES_VER, max_cards=setups.MAX_CARDS,
                  min_rr=setups.MIN_RR, hold_max=setups.HOLD_MAX, running=_setups_running.is_set(), msg=msg,
                  disc_min=setups.DISCOVERY_MIN_QUALITY, disc_sector=setups.DISCOVERY_PER_SECTOR)


@app.post("/setups/run")
def setups_run():
    if not _setups_running.is_set():
        _setups_running.set()

        def work():
            try:
                setups.run_daily(use_llm=True)
            except Exception:  # noqa: BLE001
                log.exception("setup scan failed")
            finally:
                _setups_running.clear()
        threading.Thread(target=work, daemon=True).start()
    return RedirectResponse("/setups?msg=" + quote("Tarama başladı; sayfa kendini yenileyecek."), status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "tickers": len(db.active_tickers())}


# ── plug-in routers: web/routes_<name>.py exposing `router` (APIRouter); loaded if present ──
import importlib as _importlib  # noqa: E402

for _m in ("behavior", "precheck", "tax", "earnings", "alerts", "karar", "strateji", "fikir", "tarayici"):
    try:
        app.include_router(_importlib.import_module(f".routes_{_m}", __package__).router)
    except ModuleNotFoundError as _exc:
        if _exc.name != f"{__package__}.routes_{_m}":
            raise
