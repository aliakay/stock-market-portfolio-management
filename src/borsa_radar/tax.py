"""German capital-gains tax helper ("Vergi"): FIFO lots, EUR at ECB reference rates, Anlage KAP / KAP-INV / SO.

Not tax advice — a clearly labelled estimate to prepare the self-declaration (Revolut does not withhold German tax).

Rules implemented (simplified, see the page for caveats)
- FIFO per symbol across all sub-portfolios (one broker depot). The app's portfolio ledger uses average cost
  per sub-portfolio, so realized figures differ on purpose.
- Every buy is converted to EUR at the ECB reference rate of its day, every sale at the rate of its day
  (§20 Abs. 4 S. 1 EStG). Fees are part of cost (buy) and reduce proceeds (sell). Rate = last ECB fixing on or
  before the day; Yahoo EURUSD=X closes as a fallback.
- Pots: Aktien (stock gains; stock losses offset only stock gains and carry forward), sonstige (options,
  corporate-action cash, interest, dividends; may offset everything), Fonds (ETFs → Anlage KAP-INV, 30 %
  Teilfreistellung assumed for equity funds), crypto (§23 EStG, tax free after > 1 year, Freigrenze).
- Sparer-Pauschbetrag 1,000 € (single, from 2023), Abgeltungsteuer 25 % + 5.5 % Soli, no church tax.
- Revolut dividends arrive net of 15 % US withholding (no separate tax rows); gross and creditable foreign tax
  are estimated from that unless explicit withholding rows exist.
"""

from __future__ import annotations

import csv as _csv
import io
import logging
import re
import threading
import time
import zipfile
from bisect import bisect_right
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import db, portfolio, prices
from .util import parse_dt, tr_n, utcnow_iso

log = logging.getLogger(__name__)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
BERLIN = ZoneInfo("Europe/Berlin")
KAPEST = 0.25
SOLI = 0.055
EFFECTIVE = KAPEST * (1 + SOLI)          # 26.375 %
WHT_ASSUMED = 0.15                      # US withholding with W-8BEN
TEILFREISTELLUNG = 0.30                 # equity funds (Aktienfonds)
MAX_FX_GAP = 7                          # days: an ECB fixing older than this is "stale"
REFRESH_EVERY = 6 * 3600                # auto-refresh attempts at most this often
EPS = 1e-9
DISCLAIMER = ("Vergi danışmanlığı değildir. Bu sayfa beyannameyi hazırlamak için bir tahmindir; kesin tutarlar ve "
              "satır numaraları için resmi Anlage KAP / KAP-INV / SO talimatlarına bak ya da bir Steuerberater'a danış.")
MAPPING_NOTE = "yaklaşık eşleme — resmi form talimatını kontrol et"
POTS = {
    "aktien": "Hisse (Aktien)",
    "fonds": "Fon / ETF (KAP-INV)",
    "sonstige": "Diğer (sonstige)",
    "krypto": "Kripto ≤ 1 yıl (§23)",
    "krypto_frei": "Kripto > 1 yıl (vergisiz)",
}
POT_OF_ASSET = {"stock": "aktien", "etf": "fonds", "option": "sonstige", "crypto": "krypto"}
TAX_NOTE = re.compile(r"TAX|WITHHOLD|QUELLENSTEUER", re.I)


def pauschbetrag(year: int) -> float:
    return 1000.0 if year >= 2023 else 801.0


def crypto_freigrenze(year: int) -> float:
    return 1000.0 if year >= 2024 else 600.0


# ── FX store (ECB reference rates) ────────────────────────

_schema_ok = False
_fx_lock = threading.Lock()
_last_attempt = 0.0


def _ensure_schema() -> None:
    global _schema_ok
    if _schema_ok:
        return
    db.execute("CREATE TABLE IF NOT EXISTS fx_ecb (day TEXT NOT NULL, ccy TEXT NOT NULL, rate REAL NOT NULL, "
               "PRIMARY KEY (day, ccy))")
    db.execute("CREATE TABLE IF NOT EXISTS fx_ecb_meta (key TEXT PRIMARY KEY, value TEXT)")
    _schema_ok = True


def _meta(key: str) -> str | None:
    r = db.query_one("SELECT value FROM fx_ecb_meta WHERE key = ?", (key,))
    return r["value"] if r else None


def _set_meta(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO fx_ecb_meta(key, value) VALUES (?, ?)", (key, value))


def _needed_currencies() -> set[str]:
    cur = {r["c"] for r in db.query("SELECT DISTINCT upper(currency) c FROM trades UNION SELECT DISTINCT upper(currency) c FROM cash_flows")}
    return (cur | {"USD"}) - {"EUR", None, ""}


def parse_ecb_csv(text: str, currencies: set[str] | None = None) -> list[tuple[str, str, float]]:
    """eurofxref-hist.csv → [(day, ccy, ccy per 1 EUR)]."""
    rows = list(_csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    head = [h.strip().upper() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not r or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0].strip()):
            continue
        for i, ccy in enumerate(head[1:], start=1):
            if not ccy or (currencies and ccy not in currencies) or i >= len(r):
                continue
            try:
                v = float(r[i])
            except ValueError:
                continue
            if v > 0:
                out.append((r[0].strip(), ccy, v))
    return out


def fx_refresh() -> dict:
    """Download the ECB history and cache the needed currencies. On failure fill Yahoo EURUSD=X instead."""
    global _last_attempt
    _ensure_schema()
    with _fx_lock:
        _last_attempt = time.time()
        _set_meta("last_attempt", utcnow_iso())
        try:
            r = httpx.get(ECB_URL, timeout=30, follow_redirects=True,
                          headers={"User-Agent": "borsa-radar/0.1 (personal research tool)"})
            r.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(r.content))
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            rows = parse_ecb_csv(z.read(name).decode("utf-8", "replace"), _needed_currencies())
            if not rows:
                raise ValueError("ECB dosyası boş")
            with db.tx() as c:
                c.executemany("INSERT OR REPLACE INTO fx_ecb(day, ccy, rate) VALUES (?,?,?)", rows)
            _set_meta("fetched_at", utcnow_iso())
            last = max(d for d, _, _ in rows)
            return {"ok": True, "source": "ECB", "rows": len(rows), "last_day": last}
        except Exception as exc:  # noqa: BLE001 - network / format; fall back to Yahoo
            log.warning("ECB fx download failed: %s", type(exc).__name__)
            err = f"ECB indirilemedi ({type(exc).__name__})"
        try:
            first = portfolio.first_day()
            st = date.fromisoformat(first) - timedelta(days=15) if first else None
            n = prices.refresh(["EURUSD=X"], start=st, full=True).get("EURUSD=X", 0)
            return {"ok": n > 0, "source": "Yahoo EURUSD=X", "rows": n, "error": err}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "source": None, "rows": 0, "error": f"{err}; Yahoo da başarısız ({type(exc).__name__})"}


def _maybe_refresh() -> None:
    """Fetch rates when the cache is empty or more than a few days behind (throttled)."""
    _ensure_schema()
    last = db.query_one("SELECT MAX(day) d FROM fx_ecb WHERE ccy = 'USD'")["d"]
    if last and last >= (date.today() - timedelta(days=4)).isoformat():
        return
    if time.time() - _last_attempt < REFRESH_EVERY:
        return
    att = _meta("last_attempt")
    if att and last:
        t = parse_dt(att)
        if t and (time.time() - t.timestamp()) < REFRESH_EVERY:
            return
    fx_refresh()


def fx_status() -> dict:
    _ensure_schema()
    r = db.query_one("SELECT MIN(day) a, MAX(day) b, COUNT(*) n FROM fx_ecb WHERE ccy = 'USD'")
    y = prices.series("EURUSD=X")
    return {"ecb_first": r["a"], "ecb_last": r["b"], "ecb_rows": r["n"], "fetched_at": _meta("fetched_at"),
            "yahoo_last": y.last()[0] if y else None}


class _Fx:
    """Rate lookup: ccy per 1 EUR, last value on or before the day."""

    def __init__(self):
        self.ecb: dict[str, tuple[list[str], list[float]]] = {}
        for r in db.query("SELECT day, ccy, rate FROM fx_ecb ORDER BY ccy, day"):
            d, v = self.ecb.setdefault(r["ccy"], ([], []))
            d.append(r["day"])
            v.append(r["rate"])
        self.yahoo: dict[str, prices.Series] = {}
        self.used: set[str] = set()
        self.missing: set[str] = set()
        self.stale: set[str] = set()

    @staticmethod
    def _at(days: list[str], vals: list[float], day: str) -> tuple[float, str] | None:
        i = bisect_right(days, day) - 1
        return (vals[i], days[i]) if i >= 0 else None

    def rate(self, ccy: str, day: str) -> dict | None:
        ccy = (ccy or "USD").upper()
        if ccy == "EUR":
            return {"rate": 1.0, "day": day, "src": "EUR"}
        gap = lambda d: (date.fromisoformat(day) - date.fromisoformat(d)).days  # noqa: E731
        hit = self._at(*self.ecb[ccy], day) if ccy in self.ecb else None
        if hit and gap(hit[1]) <= MAX_FX_GAP:
            self.used.add("ECB")
            return {"rate": hit[0], "day": hit[1], "src": "ECB"}
        ys = self.yahoo.get(ccy)
        if ys is None:
            ys = self.yahoo[ccy] = prices.series(f"EUR{ccy}=X")
        yh = self._at(ys.days, ys.closes, day) if ys else None
        if yh and gap(yh[1]) <= MAX_FX_GAP:
            self.used.add("Yahoo")
            return {"rate": yh[0], "day": yh[1], "src": "Yahoo"}
        best = max((x for x in (hit, yh) if x), key=lambda x: x[1], default=None)
        if best:
            self.stale.add(day)
            self.used.add("ECB" if best is hit else "Yahoo")
            return {"rate": best[0], "day": best[1], "src": ("ECB" if best is hit else "Yahoo") + " (eski)"}
        self.missing.add(day)
        return None


# ── helpers ───────────────────────────────────────────────

def tax_day(ts: str) -> str:
    """Calendar day in Germany (the taxpayer's year boundary)."""
    return parse_dt(ts).astimezone(BERLIN).date().isoformat()


def _plus_year(d: str) -> str:
    x = date.fromisoformat(d)
    try:
        return x.replace(year=x.year + 1).isoformat()
    except ValueError:              # 29 Feb
        return x.replace(year=x.year + 1, day=28).isoformat()


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _r(v, d=2):
    return None if v is None else round(v, d)


# ── engine ────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def invalidate() -> None:
    with _cache_lock:
        _cache.clear()


def _signature() -> tuple:
    a = db.query_one("SELECT COUNT(*) n, MAX(id) m, MAX(ts) t FROM trades")
    b = db.query_one("SELECT COUNT(*) n, MAX(id) m, MAX(ts) t FROM cash_flows")
    c = db.query_one("SELECT COUNT(*) n, MAX(day) d FROM fx_ecb")
    return tuple(a) + tuple(b) + tuple(c)


def _year_bucket(year: int) -> dict:
    return {"year": year, "sales": [], "income": [], "issues": [], "custody_fees_eur": 0.0, "custody_fees_n": 0,
            "open_lots": [], "avg_cost_usd": 0.0, "avg_cost_n": 0}


def _issue(b: dict, text: str, level: str = "warn", day: str | None = None) -> None:
    b["issues"].append({"level": level, "text": text, "day": day})


def _compute() -> dict:
    _ensure_schema()
    fx = _Fx()
    trades = [dict(r) for r in db.query("SELECT t.*, s.code sub FROM trades t LEFT JOIN subportfolios s ON s.id = t.sub_id")]
    cash = [dict(r) for r in db.query("SELECT c.*, s.code sub FROM cash_flows c LEFT JOIN subportfolios s ON s.id = c.sub_id")]
    for e in trades:
        e["_type"] = "trade"
    for e in cash:
        e["_type"] = "cash"
    events = trades + cash
    for e in events:
        e["_day"] = tax_day(e["ts"])
        e["_ny"] = portfolio.ny_day(e["ts"])
    order = {"cash": 0, "BUY": 1, "SELL": 2}
    events.sort(key=lambda e: (e["ts"], order.get(e.get("side") or e["_type"], 0), e["id"]))

    today = date.today()
    first_year = int(events[0]["_day"][:4]) if events else today.year
    years = {y: _year_bucket(y) for y in range(first_year, today.year + 1)}
    etf_syms = {e["symbol"] for e in trades if e["asset"] == "etf"}
    explicit_wht = any(e["kind"] in ("fee", "adjustment") and TAX_NOTE.search(e.get("note") or "") for e in cash)

    # average-cost realized from the app ledger (USD), for the comparison panel
    try:
        led_state = portfolio.build().trade_state
    except Exception:  # noqa: BLE001 - comparison only
        led_state = {}

    lots: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    split_cache: dict = {}
    cur_year = first_year

    def snapshot(year: int) -> None:
        """Open FIFO lots at 31 Dec (or today for the running year): not taxed until sold."""
        b = years.get(year)
        if b is None:
            return
        end = f"{year}-12-31" if year < today.year else today.isoformat()
        for sym, ls in sorted(lots.items()):
            q = sum(lot["qty"] for lot in ls)
            if q <= 1e-7:
                continue
            m = meta[sym]
            cost_eur = sum(lot["cost_eur"] for lot in ls) if all(lot["cost_eur"] is not None for lot in ls) else None
            cost_ccy = sum(lot["cost_ccy"] for lot in ls)
            s = prices.series(sym)
            px, px_day = None, None
            if s:
                i = bisect_right(s.days, end) - 1
                if i >= 0:
                    px, px_day = s.closes[i], s.days[i]
            fr = fx.rate(m["currency"], end)
            val_eur = px * q * m["multiplier"] / fr["rate"] if px is not None and fr else None
            row = {"symbol": sym, "asset": m["asset"], "qty": q, "lots": len(ls), "cost_ccy": cost_ccy, "cost_eur": cost_eur,
                   "currency": m["currency"], "oldest": min(lot["day"] for lot in ls), "price": px, "price_day": px_day,
                   "value_eur": val_eur, "unreal_eur": (val_eur - cost_eur) if val_eur is not None and cost_eur is not None else None,
                   "fx_day": fr and fr["day"]}
            if m["asset"] == "crypto":
                row["free_from"] = _plus_year(min(lot["day"] for lot in ls))
            if m["asset"] == "option" and m.get("expiry") and m["expiry"] < end and m["expiry"][:4] == str(year):
                _issue(b, f"{sym}: vadesi ({m['expiry']}) geçmiş açık opsiyon. Değersiz sona erdiyse Termingeschäft zararı "
                          "olarak beyan edilebilir; burada hesaba katılmadı.")
            b["open_lots"].append(row)

    for e in events:
        y = int(e["_day"][:4])
        while cur_year < y:
            snapshot(cur_year)
            cur_year += 1
        b = years.setdefault(y, _year_bucket(y))
        day = e["_day"]
        if e["_type"] == "cash":
            kind, amt, ccy = e["kind"], e["amount"], e["currency"]
            fr = fx.rate(ccy, day)
            eur = amt / fr["rate"] if fr else None
            note = e.get("note") or ""
            base = {"id": e["id"], "day": day, "symbol": e.get("symbol"), "amount": amt, "currency": ccy, "eur": eur,
                    "fx": fr, "note": note, "sub": e.get("sub")}
            if kind == "dividend":
                if TAX_NOTE.search(note):
                    b["income"].append(base | {"type": "wht_correction"})
                else:
                    typ = "fund_dist" if e.get("symbol") in etf_syms else "dividend"
                    b["income"].append(base | {"type": typ})
            elif kind == "interest":
                b["income"].append(base | {"type": "interest"})
            elif kind in ("fee", "adjustment") and TAX_NOTE.search(note):
                b["income"].append(base | {"type": "wht"})
            elif kind == "adjustment":
                if e.get("symbol"):
                    b["income"].append(base | {"type": "corp_cash"})
                    _issue(b, f"{e['symbol']}: kurumsal işlem nakdi ({note or 'adjustment'}, {tr_n(amt)} {ccy}) sıfır maliyetle "
                              "'Diğer' potuna kazanç olarak yazıldı; sınıflandırma doğrulanamadı.", "info", day)
                else:
                    _issue(b, f"Sembolsüz düzeltme kaydı ({tr_n(amt)} {ccy}, {note or '—'}) vergi hesabına alınmadı.", "info", day)
            elif kind == "fee":
                if eur is not None:
                    b["custody_fees_eur"] += -eur
                b["custody_fees_n"] += 1
            if fr is None and kind in ("dividend", "interest", "adjustment"):
                _issue(b, f"{day}: {ccy} kuru yok, {kind} tutarı EUR'ya çevrilemedi (doğrulanamadı).", "warn", day)
            continue

        # trade
        sym, asset = e["symbol"], e["asset"]
        mult = e["multiplier"] or 1.0
        meta.setdefault(sym, {"asset": asset, "currency": e["currency"], "multiplier": mult, "expiry": e.get("expiry")})
        f = prices.split_factor_after(sym, e["_ny"], split_cache) if asset in ("stock", "etf") else 1.0
        q, unit = e["qty"] * f, e["price"] / f
        fr = fx.rate(e["currency"], day)
        if fr is None:
            _issue(b, f"{sym} {day}: {e['currency']} kuru yok; EUR tutarı doğrulanamadı.", "warn", day)
        book = lots.setdefault(sym, [])
        if e["side"] == "BUY":
            cost = q * unit * mult + (e["fees"] or 0)
            if e["price"] == 0:
                _issue(b, f"{sym} {day}: fiyatı 0 olan giriş (kurumsal işlem: {e.get('note') or '—'}); maliyet 0 kabul edildi, "
                          "gerçek maliyet dağılımı doğrulanamadı.", "info", day)
            book.append({"qty": q, "qty0": q, "day": day, "cost_ccy": cost, "cost_eur": cost / fr["rate"] if fr else None,
                         "rate": fr and fr["rate"], "trade_id": e["id"]})
            continue

        held = sum(lot["qty"] for lot in book)
        fee = e["fees"] or 0
        gross = q * unit * mult
        excess = 0.0
        if q > held + 1e-6:
            excess = q - held
            _issue(b, f"{sym} {day}: eldekinden fazla satış ({q:g} > {held:g}); fazlası ({excess:g}) yok sayıldı — "
                      "eksik alım kaydı veya açığa satış olabilir.", "warn", day)
            scale = held / q if q else 0
            q, gross, fee = held, gross * scale, fee * scale
        if q <= EPS:
            continue
        proceeds_ccy = gross - fee
        proceeds_eur = proceeds_ccy / fr["rate"] if fr else None
        parts: dict[str, dict] = {}
        remaining = q
        while remaining > 1e-9 and book:
            lot = book[0]
            take = min(lot["qty"], remaining)
            share = take / lot["qty0"]
            c_ccy = lot["cost_ccy"] * share
            c_eur = lot["cost_eur"] * share if lot["cost_eur"] is not None else None
            pot = POT_OF_ASSET.get(asset, "sonstige")
            if asset == "crypto" and day > _plus_year(lot["day"]):
                pot = "krypto_frei"
            p = parts.setdefault(pot, {"qty": 0.0, "cost_ccy": 0.0, "cost_eur": 0.0, "buy_days": [], "hold": 0.0,
                                       "rates": [], "cost_eur_ok": True})
            p["qty"] += take
            p["cost_ccy"] += c_ccy
            if c_eur is None:
                p["cost_eur_ok"] = False
            else:
                p["cost_eur"] += c_eur
            p["buy_days"].append(lot["day"])
            p["hold"] += take * _days(lot["day"], day)
            if lot["rate"]:
                p["rates"].append(lot["rate"])
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-9:
                book.pop(0)
        avg = led_state.get(e["id"], {}).get("realized")
        if avg is not None:
            b["avg_cost_usd"] += avg
            b["avg_cost_n"] += 1
        for pot, p in parts.items():
            w = p["qty"] / q
            pr_ccy = proceeds_ccy * w
            pr_eur = proceeds_eur * w if proceeds_eur is not None else None
            c_eur = p["cost_eur"] if p["cost_eur_ok"] else None
            gain_eur = pr_eur - c_eur if pr_eur is not None and c_eur is not None else None
            b["sales"].append({
                "id": e["id"], "ts": e["ts"], "day": day, "symbol": sym, "asset": asset, "pot": pot, "pot_label": POTS[pot],
                "sub": e.get("sub"), "qty": p["qty"], "price": unit, "currency": e["currency"], "fees": fee * w,
                "cost_ccy": p["cost_ccy"], "proceeds_ccy": pr_ccy, "gain_ccy": pr_ccy - p["cost_ccy"],
                "cost_eur": c_eur, "proceeds_eur": pr_eur, "gain_eur": gain_eur,
                "sell_rate": fr and fr["rate"], "sell_rate_day": fr and fr["day"], "sell_rate_src": fr and fr["src"],
                "buy_rate_min": min(p["rates"]) if p["rates"] else None, "buy_rate_max": max(p["rates"]) if p["rates"] else None,
                "first_buy": min(p["buy_days"]), "last_buy": max(p["buy_days"]), "lots": len(p["buy_days"]),
                "hold_days": p["hold"] / p["qty"] if p["qty"] else None,
                "hold_min": _days(max(p["buy_days"]), day), "hold_max": _days(min(p["buy_days"]), day),
                "excess": excess if pot == next(iter(parts)) else 0.0,
            })
    while cur_year <= today.year:
        snapshot(cur_year)
        cur_year += 1

    _settle(years, explicit_wht)
    st = fx_status()
    return {"years": years, "fx": st, "fx_used": sorted(fx.used), "fx_stale": sorted(fx.stale), "fx_missing": sorted(fx.missing),
            "explicit_wht": explicit_wht, "first_year": first_year}


def _sum(xs) -> float:
    return sum(x for x in xs if x is not None)


def _settle(years: dict[int, dict], explicit_wht: bool) -> None:
    """Per-year pot totals, loss offsetting and carry-forward (in year order)."""
    carry_s = carry_o = carry_k = 0.0
    for y in sorted(years):
        b = years[y]
        pots = {}
        for k in POTS:
            rows = [s for s in b["sales"] if s["pot"] == k]
            g = [s["gain_eur"] for s in rows if s["gain_eur"] is not None]
            pots[k] = {"label": POTS[k], "n": len(rows), "gains": _sum(x for x in g if x > 0), "losses": _sum(x for x in g if x < 0),
                       "net": _sum(g), "proceeds": _sum(s["proceeds_eur"] for s in rows), "cost": _sum(s["cost_eur"] for s in rows),
                       "unverified": sum(1 for s in rows if s["gain_eur"] is None),
                       "gain_ccy": _sum(s["gain_ccy"] for s in rows if s["currency"] == "USD")}
        fonds_taxable = pots["fonds"]["net"] * (1 - TEILFREISTELLUNG)
        pots["fonds"]["taxable"] = fonds_taxable

        inc = b["income"]
        div_net = _sum(i["eur"] for i in inc if i["type"] == "dividend")
        corr = _sum(i["eur"] for i in inc if i["type"] == "wht_correction")
        wht_rows = -_sum(i["eur"] for i in inc if i["type"] == "wht")
        if explicit_wht:
            div_gross, wht = div_net, wht_rows - corr
        else:
            div_gross = div_net / (1 - WHT_ASSUMED)
            wht = div_gross - div_net - corr
        fund_dist = _sum(i["eur"] for i in inc if i["type"] == "fund_dist")
        interest = _sum(i["eur"] for i in inc if i["type"] == "interest")
        corp_cash = _sum(i["eur"] for i in inc if i["type"] == "corp_cash")
        divs: dict[str, dict] = {}
        for i in inc:
            if i["type"] in ("dividend", "wht_correction", "fund_dist"):
                d = divs.setdefault(i["symbol"] or "—", {"symbol": i["symbol"] or "—", "n": 0, "net_ccy": 0.0, "net_eur": 0.0,
                                                          "currency": i["currency"], "fund": i["type"] == "fund_dist"})
                d["n"] += 1
                d["net_ccy"] += i["amount"]
                d["net_eur"] += i["eur"] or 0
        for d in divs.values():
            d["gross_eur"] = d["net_eur"] if explicit_wht or d["fund"] else d["net_eur"] / (1 - WHT_ASSUMED)
            d["wht_eur"] = d["gross_eur"] - d["net_eur"]
        b["dividends"] = sorted(divs.values(), key=lambda d: -d["net_eur"])
        b["div"] = {"net": div_net + corr, "gross": div_gross, "wht": max(0.0, wht), "assumed": not explicit_wht,
                    "n": sum(1 for i in inc if i["type"] == "dividend"), "fund_dist": fund_dist,
                    "fund_dist_taxable": fund_dist * (1 - TEILFREISTELLUNG)}
        b["interest"] = interest
        b["corp_cash"] = corp_cash

        # Abgeltungsteuer pots
        S = pots["aktien"]["net"]
        other_trades = pots["sonstige"]["net"] + corp_cash
        O = other_trades + fonds_taxable + div_gross + b["div"]["fund_dist_taxable"] + interest
        steps = {"stock_net": S, "other_trades": other_trades, "other_total": O, "carry_s_in": carry_s, "carry_o_in": carry_o}
        if S >= 0:
            used_s = min(S, carry_s)
            S1 = S - used_s
            carry_s_out = carry_s - used_s
        else:
            used_s, S1, carry_s_out = 0.0, 0.0, carry_s - S
        if O < 0:
            off = min(-O, S1)
            S1 -= off
            o_loss_left, Opos = -O - off, 0.0
        else:
            off, o_loss_left, Opos = 0.0, 0.0, O
        pos = Opos + S1
        used_o = min(pos, carry_o)
        pos -= used_o
        carry_o_out = carry_o - used_o + o_loss_left
        pb = min(pauschbetrag(y), pos)
        taxable = pos - pb
        kap_gross = KAPEST * taxable
        credit = min(b["div"]["wht"], kap_gross)
        kapest = kap_gross - credit
        soli = SOLI * kapest
        steps.update(used_s=used_s, other_offsets_stock=off, used_o=used_o, base=pos, pauschbetrag=pb,
                     pauschbetrag_max=pauschbetrag(y), taxable=taxable, kapest_gross=kap_gross, credit=credit,
                     kapest=kapest, soli=soli, tax=kapest + soli, carry_s_out=carry_s_out, carry_o_out=carry_o_out,
                     tax_no_credit=taxable * EFFECTIVE)

        # §23 crypto
        K = pots["krypto"]["net"]
        fg = crypto_freigrenze(y)
        if K <= 0:
            so_taxable, carry_k_out, fg_applied = 0.0, carry_k - K, False
        elif K < fg:
            so_taxable, carry_k_out, fg_applied = 0.0, carry_k, True
        else:
            used_k = min(K, carry_k)
            so_taxable, carry_k_out, fg_applied = K - used_k, carry_k - used_k, False
        b["crypto"] = {"net": K, "freigrenze": fg, "freigrenze_applied": fg_applied, "taxable": so_taxable,
                       "carry_in": carry_k, "carry_out": carry_k_out, "tax_free": pots["krypto_frei"]["net"]}
        carry_s, carry_o, carry_k = carry_s_out, carry_o_out, carry_k_out
        b["pots"] = pots
        b["steps"] = steps
        b["mapping"] = _mapping(b)
        b["fifo_usd"] = _sum(s["gain_ccy"] for s in b["sales"] if s["currency"] == "USD")
        b["fifo_eur"] = _sum(s["gain_eur"] for s in b["sales"])
        b["fx_effect"] = None
        usd_sales = [s for s in b["sales"] if s["currency"] == "USD" and s["gain_eur"] is not None and s["sell_rate"]]
        if usd_sales:   # EUR gain minus the USD gain translated at each sale's rate
            b["fx_effect"] = _sum(s["gain_eur"] - s["gain_ccy"] / s["sell_rate"] for s in usd_sales)


def _mapping(b: dict) -> list[dict]:
    p, st, d = b["pots"], b["steps"], b["div"]
    S = p["aktien"]["net"]
    other = p["sonstige"]["net"] + b["corp_cash"]
    opt = [s for s in b["sales"] if s["asset"] == "option"]
    opt_net = _sum(s["gain_eur"] for s in opt)
    kap = d["gross"] + b["interest"] + max(0.0, other) + max(0.0, S)
    rows = [
        {"form": "Anlage KAP", "field": "Kapitalerträge, die nicht dem inländischen Steuerabzug unterlegen haben "
                                        "(ausländische Kapitalerträge)", "value": kap,
         "note": "brüt temettü + faiz + kâr eden hisse/diğer satış netleri (KAP-INV hariç)"},
        {"form": "Anlage KAP", "field": "davon: Gewinne aus Aktienveräußerungen", "value": max(0.0, S),
         "note": "hisse potu yıl içi net kârı (pozitifse)"},
        {"form": "Anlage KAP", "field": "Verluste aus der Veräußerung von Aktien", "value": max(0.0, -S),
         "note": "hisse potu yıl içi net zararı (pozitif sayı olarak)"},
        {"form": "Anlage KAP", "field": "Verluste ohne Verluste aus der Veräußerung von Aktien", "value": max(0.0, -other),
         "note": "opsiyon/kurumsal işlem vb. net zarar"},
    ]
    if opt:
        rows.append({"form": "Anlage KAP", "field": "davon: Gewinne / Verluste aus Termingeschäften", "value": opt_net,
                     "note": "opsiyon satışları (yukarıdaki toplamların içinde)"})
    rows.append({"form": "Anlage KAP", "field": "Anrechenbare noch nicht angerechnete ausländische Steuern", "value": d["wht"],
                 "note": "ABD stopajı " + ("(tahmini: net temettü / 0,85 × %15)" if d["assumed"] else "(kayıtlardan)")})
    if p["fonds"]["n"] or d["fund_dist"]:
        rows.append({"form": "Anlage KAP-INV", "field": "Gewinne/Verluste aus der Veräußerung von Anteilen an Aktienfonds",
                     "value": p["fonds"]["net"], "note": "Teilfreistellung öncesi brüt; %30'u Finanzamt düşer (hisse fonu varsayımı)"})
        rows.append({"form": "Anlage KAP-INV", "field": "Ausschüttungen aus Aktienfonds", "value": d["fund_dist"],
                     "note": "ETF dağıtımları (brüt)"})
    if p["krypto"]["n"] or p["krypto_frei"]["n"]:
        rows.append({"form": "Anlage SO", "field": "Private Veräußerungsgeschäfte – andere Wirtschaftsgüter (Kryptowerte): "
                                                   "Gewinn/Verlust", "value": p["krypto"]["net"],
                     "note": "yalnız 1 yıl içinde satılan lotlar; >1 yıl vergisiz"})
    return rows


def _all() -> dict:
    sig = _signature() if _schema_ok else None
    with _cache_lock:
        hit = _cache.get("all")
        if hit and sig is not None and hit[0] == sig and time.time() - hit[1] < 300:
            return hit[2]
    data = _compute()
    with _cache_lock:
        _cache["all"] = (_signature(), time.time(), data)
    return data


# ── public API ────────────────────────────────────────────

def report(year: int | None = None, *, refresh_fx: bool = True) -> dict:
    """Everything the page / CLI needs for one calendar year."""
    _ensure_schema()
    if refresh_fx:
        try:
            _maybe_refresh()
        except Exception as exc:  # noqa: BLE001 - page must render without network
            log.warning("fx auto refresh failed: %s", type(exc).__name__)
    data = _all()
    today = date.today()
    yrs = sorted(data["years"])
    year = int(year or today.year)
    out = {"year": year, "years": yrs, "running": year == today.year, "today": today.isoformat(), "disclaimer": DISCLAIMER,
           "mapping_note": MAPPING_NOTE, "fx": data["fx"], "fx_used": data["fx_used"], "explicit_wht": data["explicit_wht"],
           "rates": {"kapest": KAPEST, "soli": SOLI, "effective": EFFECTIVE, "wht": WHT_ASSUMED, "tf": TEILFREISTELLUNG,
                     "pauschbetrag": pauschbetrag(year), "freigrenze": crypto_freigrenze(year)}}
    b = data["years"].get(year)
    if b is None:
        return out | {"empty": True}
    sales = sorted(b["sales"], key=lambda s: (s["day"], s["id"]))
    issues = list(b["issues"])
    stale = [d for d in data["fx_stale"] if d.startswith(str(year))]
    missing = [d for d in data["fx_missing"] if d.startswith(str(year))]
    if missing:
        issues.insert(0, {"level": "warn", "day": None, "text": f"{len(missing)} gün için kur bulunamadı (ör. {missing[0]}); "
                                                                 "bu işlemler doğrulanamadı. `borsa-radar vergi fx-refresh` dene."})
    if stale:
        issues.insert(0, {"level": "warn", "day": None, "text": f"{len(stale)} gün için güncel ECB kuru yok, en son kur kullanıldı "
                                                               f"(ör. {stale[0]})."})
    if "Yahoo" in data["fx_used"]:
        issues.append({"level": "info", "day": None, "text": "Bazı günlerde ECB kuru yerine Yahoo EURUSD=X kapanışı kullanıldı "
                                                             "(resmi değil)."})
    if b["custody_fees_n"]:
        issues.append({"level": "info", "day": None, "text": f"{b['custody_fees_n']} saklama ücreti ({tr_n(b['custody_fees_eur'])} €) "
                                                             "düşülmedi: Sparer-Pauschbetrag dışında gider (Werbungskosten) indirilemez."})
    if b["div"]["assumed"] and b["div"]["n"]:
        issues.append({"level": "info", "day": None, "text": "Revolut temettüleri net (ABD %15 stopajı düşülmüş) gösteriyor; brüt tutar "
                                                             "ve mahsup edilebilir stopaj tahmini. ABD dışı şirketlerde oran farklı "
                                                             "olabilir; Revolut yıllık vergi raporuyla karşılaştır."})
    tx_n = sum(1 for _ in sales)
    st = b["steps"]
    summary = (f"{year}{' (yıl bitmedi, ara tahmin)' if out['running'] else ''}: {len({s['id'] for s in sales})} satış · hisse potu "
               f"{tr_n(b['pots']['aktien']['net'], 2, sign=True)} € · diğer {tr_n(st['other_total'], 2, sign=True)} € · "
               f"vergiye tabi {tr_n(st['taxable'])} € · tahmini vergi {tr_n(st['tax'])} €")
    last_rate_day = max((s["sell_rate_day"] for s in sales if s["sell_rate_day"]), default=None)
    return out | {
        "empty": False, "sales": sales, "n_sales": tx_n, "pots": b["pots"], "steps": st, "div": b["div"], "dividends": b["dividends"],
        "interest": b["interest"], "corp_cash": b["corp_cash"], "crypto": b["crypto"], "mapping": b["mapping"],
        "open_lots": b["open_lots"], "issues": issues, "summary": summary, "custody_fees_eur": b["custody_fees_eur"],
        "compare": {"avg_cost_usd": b["avg_cost_usd"] if b["avg_cost_n"] else None, "fifo_usd": b["fifo_usd"],
                    "fifo_eur": b["fifo_eur"], "fx_effect": b["fx_effect"]},
        "last_rate_day": last_rate_day,
        "open_unreal_eur": _sum(x["unreal_eur"] for x in b["open_lots"]) if b["open_lots"] else None,
    }


def brief(r: dict | None) -> dict | None:
    """Compact version for context snapshots / Hermes / --json."""
    if not r or r.get("empty"):
        return None
    st, p = r["steps"], r["pots"]
    return {
        "year": r["year"], "running_year": r["running"], "currency": "EUR", "method": "FIFO, ECB reference rate per transaction day",
        "n_sales": len({s["id"] for s in r["sales"]}),
        "stock_pot_net_eur": _r(p["aktien"]["net"]), "stock_gains_eur": _r(p["aktien"]["gains"]), "stock_losses_eur": _r(p["aktien"]["losses"]),
        "other_pot_net_eur": _r(st["other_total"]), "fund_pot_net_eur": _r(p["fonds"]["net"]),
        "dividends_net_eur": _r(r["div"]["net"]), "dividends_gross_eur": _r(r["div"]["gross"]),
        "foreign_wht_eur": _r(r["div"]["wht"]), "wht_estimated": r["div"]["assumed"], "interest_eur": _r(r["interest"]),
        "pauschbetrag_used_eur": _r(st["pauschbetrag"]), "taxable_eur": _r(st["taxable"]), "est_tax_eur": _r(st["tax"]),
        "carry_forward_out": {"stock_eur": _r(st["carry_s_out"]), "other_eur": _r(st["carry_o_out"]),
                              "crypto_eur": _r(r["crypto"]["carry_out"])},
        "crypto_s23_net_eur": _r(r["crypto"]["net"]), "crypto_taxable_eur": _r(r["crypto"]["taxable"]),
        "avg_cost_realized_usd": _r(r["compare"]["avg_cost_usd"]), "fifo_realized_usd": _r(r["compare"]["fifo_usd"]),
        "open_lots_unrealized_eur": _r(r["open_unreal_eur"]),
        "mapping": [{"form": m["form"], "field": m["field"], "eur": _r(m["value"])} for m in r["mapping"]],
        "mapping_note": MAPPING_NOTE, "fx_source": ", ".join(r["fx_used"]) or None, "fx_last_day": r["fx"]["ecb_last"],
        "issues": [i["text"] for i in r["issues"]], "summary": r["summary"], "disclaimer": DISCLAIMER,
    }


def _de(v, d=2) -> str:
    """German/Turkish spreadsheet number: decimal comma, no thousands separator."""
    return "" if v is None else f"{v:.{d}f}".replace(".", ",")


def csv(year: int | None = None) -> str:
    """Per-sale and income rows for one year (';' separated, decimal comma — opens directly in DE/TR Excel)."""
    r = report(year, refresh_fx=False)
    buf = io.StringIO()
    w = _csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["tarih", "tur", "sembol", "varlik", "pot", "adet", "para_birimi", "maliyet_pb", "gelir_pb", "kazanc_pb",
                "maliyet_eur", "gelir_eur", "kazanc_eur", "elde_tutma_gun", "ilk_alis", "son_alis", "satis_kuru", "kur_tarihi",
                "kur_kaynagi", "not"])
    if r.get("empty"):
        return buf.getvalue()
    for s in r["sales"]:
        w.writerow([s["day"], "satis", s["symbol"], s["asset"], s["pot"], _de(s["qty"], 6), s["currency"], _de(s["cost_ccy"]),
                    _de(s["proceeds_ccy"]), _de(s["gain_ccy"]), _de(s["cost_eur"]), _de(s["proceeds_eur"]), _de(s["gain_eur"]),
                    _de(s["hold_days"], 0), s["first_buy"], s["last_buy"], _de(s["sell_rate"], 4), s["sell_rate_day"] or "",
                    s["sell_rate_src"] or "", f"fazla satis {s['excess']:g} yok sayildi" if s["excess"] else ""])
    data = _all()["years"][r["year"]]
    for i in sorted(data["income"], key=lambda i: i["day"]):
        fr = i["fx"] or {}
        w.writerow([i["day"], i["type"], i["symbol"] or "", "", "sonstige", "", i["currency"], "", _de(i["amount"]), "", "", _de(i["eur"]),
                    "", "", "", "", _de(fr.get("rate"), 4), fr.get("day", ""), fr.get("src", ""), i["note"]])
    st = r["steps"]
    w.writerow([])
    for k, v in (("hisse_potu_net_eur", r["pots"]["aktien"]["net"]), ("diger_toplam_eur", st["other_total"]),
                 ("temettu_brut_eur", r["div"]["gross"]), ("yabanci_stopaj_eur", r["div"]["wht"]),
                 ("sparer_pauschbetrag_eur", st["pauschbetrag"]), ("vergiye_tabi_eur", st["taxable"]), ("tahmini_vergi_eur", st["tax"])):
        w.writerow([f"{r['year']}", "toplam", k, "", "", "", "EUR", "", "", "", "", "", _de(v)])
    w.writerow(["", "not", "Vergi danismanligi degildir; tahmindir."])
    return buf.getvalue()
