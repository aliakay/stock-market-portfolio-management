"""İşlem öncesi kontrol: pre-trade discipline check + position sizer.

A recommendation tool, never a gate: it does not block or place orders. Every check returns a status
(ok / warn / stop, or na when the data to judge is missing → "doğrulanamadı") and a Turkish reason that
carries its numbers, their source and date. The verdict is GEÇ (all ok), DİKKAT (any warn) or DUR (any stop).

Rules borrowed (re-implemented) from open-source trading skills (tradermonty/claude-trading-skills, MIT):
fixed-fractional position sizing, a drawdown circuit breaker (losing-streak cooldown, weekly / monthly
realized-loss halts) and a pre-trade discipline gate (plan before entry, no revenge trades). No LLM.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np

from . import config, db, portfolio, prices, risk
from .util import tr_n

ATR_MULT = 2.0             # fallback stop = price − 2 × ATR(14)
SIZE_STOP_RATIO = 2.0      # planned risk above 2× the budget → DUR
EARN_DAYS = 5              # earnings within this many trading days → warn
REVENGE_DAYS = 2           # losing exit within this many trading days → warn
STREAK_DAYS = 5            # two losing round trips closed within this many days → cooldown
WEEK_LOSS_PCT = 5.0        # realized loss in the last 7 days above this % of the portfolio → DUR
MONTH_LOSS_PCT = 8.0       # month-to-date realized loss above this % → DUR
SHORT_HOLD_DAYS = 20       # selling a winner within this many trading days → note

STATUS_RANK = {"ok": 0, "na": 0, "warn": 1, "stop": 2}
STATUS_TR = {"ok": "uygun", "na": "doğrulanamadı", "warn": "dikkat", "stop": "dur"}
VERDICT = {0: ("GEÇ", "pos"), 1: ("DİKKAT", "warn"), 2: ("DUR", "neg")}
NA = "doğrulanamadı"


# ── small helpers ─────────────────────────────────────────

def _today() -> date:
    return datetime.now(config.NY_TZ).date()


def _tdays(a: date | str, b: date | str) -> int:
    """Weekdays from a (inclusive) to b (exclusive); market holidays are not excluded."""
    a = date.fromisoformat(a) if isinstance(a, str) else a
    b = date.fromisoformat(b) if isinstance(b, str) else b
    return int(np.busday_count(a, b)) if a <= b else -int(np.busday_count(b, a))


def _usd(v: float | None, d: int = 0) -> str:
    if v is None:
        return "—"
    return ("−" if v < 0 else "") + tr_n(abs(v), d) + "$"


def _px(v: float | None) -> str:
    return _usd(v, 2)


def _q(v: float | None) -> str:
    if v is None:
        return "—"
    return tr_n(v, 0) if abs(v - round(v)) < 1e-6 else tr_n(v, 2)


def _pc(v: float | None, d: int = 1) -> str:
    """Signed Turkish percent: %+2,3 / %−31,4"""
    return "—" if v is None else "%" + tr_n(v, d, sign=True).replace("-", "−")


def _lower(s: str) -> str:
    return s.replace("İ", "i").replace("I", "ı").lower()


def _d(day: str | None) -> str:
    """2026-11-13 → 13.11.2026"""
    if not day:
        return "—"
    return f"{day[8:10]}.{day[5:7]}.{day[:4]}"


def _floor_qty(x: float) -> float:
    if x <= 0:
        return 0.0
    return float(math.floor(x)) if x >= 1 else math.floor(x * 100) / 100


def _check(key: str, title: str, status: str, reason: str, source: str = "", **extra) -> dict:
    return {"key": key, "title": title, "status": status, "status_tr": STATUS_TR[status], "reason": reason,
            "source": source, **extra}


# ── data ──────────────────────────────────────────────────

def _price(sym: str) -> tuple[float | None, str | None]:
    s = prices.series(sym)
    if not s:
        try:  # not in the store yet (new idea): fetch ~2y of daily bars once
            from . import technicals
            technicals.ensure_bars(sym)
            s = prices.series(sym)
        except Exception:  # noqa: BLE001
            pass
    last = s.last() if s else None
    return (last[1], last[0]) if last else (None, None)


def _atr(sym: str, today: date) -> tuple[float | None, str | None]:
    from . import technicals
    bs = [b for b in prices.bars(sym, (today - timedelta(days=150)).isoformat()) if b["close"] is not None]
    a = technicals.atr(bs, 14) if bs else None
    return a, (bs[-1]["day"] if bs else None)


def _open_setup(sym: str) -> dict | None:
    try:
        from . import setups
        return next((s for s in setups.open_setups() if s["symbol"] == sym), None)
    except Exception:  # noqa: BLE001
        return None


def _holding(sym: str, pos: list[dict]) -> dict | None:
    rows = [p for p in pos if p["symbol"] == sym]
    if not rows:
        return None
    qty = sum(p["qty"] for p in rows)
    cost = sum(p["cost"] for p in rows)
    value = sum(p["value"] for p in rows)
    mult = rows[0]["multiplier"] or 1
    first = min((p["first_day"] for p in rows if p["first_day"]), default=None)
    return {"qty": qty, "cost": cost, "avg": cost / (qty * mult) if qty else None, "value": value, "first_day": first,
            "unreal_pct": (value / cost - 1) * 100 if cost > 0 else None, "price_day": rows[0]["price_day"],
            "subs": sorted({p["sub"] for p in rows if p["sub"]})}


def _earnings(sym: str, today: date) -> dict | None:
    """Next earnings date: radar's calendar (Finnhub, watchlist) first, else Yahoo's cached calendar."""
    try:
        from .collectors import macro
        for r in macro.upcoming(24 * 120):
            r = dict(r)
            if r.get("key") == f"earnings:{sym}" or (r.get("name") or "").startswith(f"{sym} bilanço"):
                when = datetime.fromisoformat(r["release_at"]).astimezone(config.NY_TZ)
                if when.date() < today:
                    continue
                return {"day": when.date().isoformat(), "time": when.strftime("%H:%M"), "name": r["name"],
                        "source": "radar takvimi (Finnhub)"}
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import stockdata
        f = stockdata.yahoo("calendar", sym, ttl_hours=24)
        raw = (f.data or {}).get("Earnings Date") if isinstance(f.data, dict) else None
        days = sorted(str(x)[:10] for x in (raw if isinstance(raw, list) else [raw] if raw else []))
        nxt = next((d for d in days if d >= today.isoformat()), None)
        if nxt:
            return {"day": nxt, "time": None, "name": f"{sym} bilanço", "source": f"Yahoo takvimi ({(f.fetched_at or '')[:10]})"}
    except Exception:  # noqa: BLE001
        pass
    return None


def _corr_with_holdings(sym: str, holdings: list[str], as_of: str | None) -> tuple[dict[str, float], int]:
    """Correlation of daily returns between `sym` and each holding over the last WIN_CORR sessions."""
    if not holdings:
        return {}, 0
    cal = [d for d in prices.series(config.CALENDAR_SYMBOL).days if not as_of or d <= as_of]
    days = cal[-(risk.WIN_CORR + 1):]
    if len(days) < 41:
        return {}, 0
    ser = prices.series_many([sym] + holdings, start=days[0])
    r0 = risk._returns(ser[sym], days)[1:]
    out, n_max = {}, 0
    for h in holdings:
        if h == sym:
            continue
        r1 = risk._returns(ser[h], days)[1:]
        pairs = [(a, b) for a, b in zip(r0, r1) if a is not None and b is not None]
        if len(pairs) < 40:
            continue
        x, y = np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])
        if x.std() == 0 or y.std() == 0:
            continue
        out[h] = float(np.corrcoef(x, y)[0, 1])
        n_max = max(n_max, len(pairs))
    return out, n_max


def _realized_since(led: portfolio.Ledger, day0: str) -> tuple[float, int]:
    """Net realized $ from every sell (partial ones too) on/after the NY day `day0`."""
    total, n = 0.0, 0
    for r in db.query("SELECT id, ts FROM trades WHERE side = 'SELL' AND ts >= ?", ((date.fromisoformat(day0) - timedelta(days=1)).isoformat(),)):
        if portfolio.ny_day(r["ts"]) < day0:
            continue
        x = led.trade_state.get(r["id"], {}).get("realized")
        if x is not None:
            total += x
            n += 1
    return total, n


# ── the check ─────────────────────────────────────────────

def check(side: str, symbol: str, qty: float | None = None, amount: float | None = None, price: float | None = None,
          stop: float | None = None, *, today: date | str | None = None) -> dict:
    side = (side or "").strip().lower()
    side = {"al": "buy", "alış": "buy", "sat": "sell", "satış": "sell"}.get(side, side)
    if side not in ("buy", "sell"):
        raise ValueError("Yön 'buy' (al) ya da 'sell' (sat) olmalı.")
    sym = (symbol or "").strip().upper().replace(" ", "")
    if not sym:
        raise ValueError("Sembol gerekli.")
    for name, v in (("adet", qty), ("tutar", amount), ("fiyat", price), ("stop", stop)):
        if v is not None and (not isinstance(v, (int, float)) or v != v or v <= 0):
            raise ValueError(f"{name} pozitif bir sayı olmalı.")
    today = date.fromisoformat(today) if isinstance(today, str) else (today or _today())

    last_px, last_day = _price(sym)
    px = price or last_px
    px_src = "girilen fiyat" if price else (f"son kapanış {_d(last_day)}, Yahoo" if last_px else NA)

    led = portfolio.build()
    total = led.total[-1]["value"] if led.total else None
    cash = led.total[-1]["cash"] if led.total else 0.0
    pf_day = led.days[-1] if led.days else None
    pos = portfolio.positions(led=led) if led.days else []
    hold = _holding(sym, pos)

    r: dict = {"side": side, "side_tr": "Alış" if side == "buy" else "Satış", "symbol": sym, "today": today.isoformat(),
               "price": px, "price_src": px_src, "last_close": last_px, "last_close_day": last_day,
               "qty_in": qty, "amount_in": amount, "stop_in": stop,
               "portfolio": {"value": total, "cash": cash, "day": pf_day, "src": f"radar defteri, {_d(pf_day)} kapanışı" if pf_day else NA},
               "holding": hold, "checks": [], "context": {}, "notes": []}
    r.update(dict.fromkeys(("qty", "qty_src", "amount", "stop", "stop_src", "budget", "per_share_risk", "suggested_qty",
                            "suggested_risk", "planned_risk", "risk_ratio", "max_by_risk", "max_by_weight", "post_total",
                            "weight_after", "sector", "industry", "sector_after", "cluster", "regime", "earnings", "thesis",
                            "est_realized", "est_pct", "hold_days", "hold_tdays", "realized_7d", "realized_mtd", "atr")))
    r.update({"settings": {}, "correlations": [], "corr_days": 0, "recent_trips": []})
    r["context"].update(_context(sym, px, last_day, hold))
    if side == "buy":
        _buy(r, led, pos, hold, qty, amount, stop, today)
    else:
        _sell(r, led, hold, qty, amount, today)

    worst = max((STATUS_RANK[c["status"]] for c in r["checks"]), default=0)
    r["verdict"], r["tone"] = VERDICT[worst]
    r["n_warn"] = sum(c["status"] == "warn" for c in r["checks"])
    r["n_stop"] = sum(c["status"] == "stop" for c in r["checks"])
    r["n_na"] = sum(c["status"] == "na" for c in r["checks"])
    r["summary"] = _summary(r)
    r["sources"] = ["radar defteri", "Yahoo günlük kapanış", "radar rejim", "radar takvimi", "tez defteri", "setup tarayıcı"]
    return r


def _context(sym: str, px: float | None, last_day: str | None, hold: dict | None) -> dict:
    ctx: dict = {}
    try:
        from . import scorecard
        sc = scorecard.latest(sym)
        if sc:
            ctx["karne"] = {"overall": sc.get("overall"), "label": sc.get("label"), "tone": sc.get("tone"),
                            "as_of": sc.get("as_of"), "created": (sc.get("created_at") or "")[:10]}
    except Exception:  # noqa: BLE001
        pass
    s = _open_setup(sym)
    if s:
        ctx["setup"] = {"day": s["day"], "kind": s.get("name_tr") or s["kind"], "status": s.get("status_tr") or s["status"],
                        "entry_lo": s["entry_lo"], "entry_hi": s["entry_hi"], "stop": s["stop"], "t1": s["t1"], "t2": s.get("t2"),
                        "id": s["id"]}
    if hold:
        ctx["holding"] = hold
    return ctx


def _buy(r: dict, led, pos: list[dict], hold: dict | None, qty, amount, stop, today: date) -> None:
    sym, px, total, cash = r["symbol"], r["price"], r["portfolio"]["value"], r["portfolio"]["cash"] or 0.0
    rpt = _setting_pct("risk_per_trade_pct", 1.0)
    maxpos = _setting_pct("max_position_pct", 20.0)
    r["settings"] = {"risk_per_trade_pct": rpt, "max_position_pct": maxpos}

    # ── stop: given → open setup → price − 2×ATR(14) ──
    stop_src = None
    setup = r["context"].get("setup")
    if stop:
        stop_src = "girilen stop"
    elif setup and setup.get("stop"):
        stop, stop_src = setup["stop"], f"açık setup stopu: {setup['kind']}, {_d(setup['day'])}"
    elif px:
        atr, atr_day = _atr(sym, today)
        if atr:
            stop = px - ATR_MULT * atr
            stop_src = f"fiyat − 2×ATR(14); ATR {_px(atr)} = %{tr_n(atr / px * 100, 1)}, {_d(atr_day)} kapanışı"
            r["atr"] = atr
    r["stop"], r["stop_src"] = stop, stop_src or NA
    if setup and px and not (setup["entry_lo"] * 0.995 <= px <= setup["entry_hi"] * 1.005):
        where = "altında" if px < setup["entry_lo"] else "üstünde"
        r["notes"].append(f"Fiyat {_px(px)}, açık setup'ın giriş bölgesinin ({_px(setup['entry_lo'])}–{_px(setup['entry_hi'])}) {where}; "
                          "setup henüz bu fiyattan bir giriş önermiyor.")

    budget = total * rpt / 100 if total else None
    per_share = px - stop if px and stop else None
    existing = hold["value"] if hold else 0.0
    r["budget"], r["per_share_risk"] = budget, per_share

    # max shares by risk and by the position-weight limit (buying beyond cash grows the portfolio by the shortfall)
    max_by_risk = budget / per_share if budget and per_share and per_share > 0 else None
    max_by_weight = None
    if total and px:
        m = maxpos / 100
        a = m * total - existing
        if a > cash:
            a = (m * (total - cash) - existing) / (1 - m) if m < 1 else math.inf
        max_by_weight = max(a, 0) / px
    cands = [x for x in (max_by_risk, max_by_weight) if x is not None]
    suggested = _floor_qty(min(cands)) if cands else None
    r["max_by_risk"], r["max_by_weight"], r["suggested_qty"] = max_by_risk, max_by_weight, suggested
    r["suggested_risk"] = suggested * per_share if suggested is not None and per_share and per_share > 0 else None

    # planned quantity
    if qty:
        r["qty"], r["qty_src"] = float(qty), "girilen adet"
    elif amount and px:
        r["qty"], r["qty_src"] = amount / px, f"{_usd(amount)} tutar ÷ {_px(px)}"
    else:
        r["qty"], r["qty_src"] = suggested, "önerilen adet (adet/tutar girilmedi)"
    q = r["qty"]
    amt = q * px if q and px else None
    r["amount"] = amt
    post_total = total + max(0.0, (amt or 0) - cash) if total else None
    r["post_total"] = post_total
    if amt and amt > cash + 1:
        r["notes"].append(f"Nakit {_usd(cash)}; alımın {_usd(amt - cash)} kısmı yeni para gerektirir. Ağırlıklar bu tutar eklenmiş "
                          f"portföye ({_usd(post_total)}) göre hesaplandı.")
    if hold and hold["unreal_pct"] is not None and px and hold["avg"] and px < hold["avg"] * 0.98:
        r["notes"].append(f"Ekleme maliyetin altında (ortalama düşürme): maliyet {_px(hold['avg'])}, fiyat {_px(px)} "
                          f"({_pc((px / hold['avg'] - 1) * 100)}).")

    # 1. sizing
    if not px:
        r["checks"].append(_check("size", "Pozisyon büyüklüğü", "na", f"Fiyat {NA}: {sym} için kayıtlı kapanış yok; fiyatı elle gir."))
    elif not total:
        r["checks"].append(_check("size", "Pozisyon büyüklüğü", "na", f"Portföy değeri {NA} (defter boş)."))
    elif not stop:
        r["checks"].append(_check("size", "Pozisyon büyüklüğü", "na",
                                  f"Stop {NA}: girilmedi, açık setup yok, ATR için yeterli kapanış yok. Stop girersen boyut hesaplanır."))
    elif per_share <= 0:
        r["checks"].append(_check("size", "Pozisyon büyüklüğü", "stop",
                                  f"Stop {_px(stop)} giriş fiyatının ({_px(px)}) üstünde ya da eşit: alışta risk tanımsız. Stopu düzelt.",
                                  r["stop_src"]))
    else:
        head = (f"Risk bütçesi: portföy {_usd(total)} × %{tr_n(rpt, 1)} = {_usd(budget)}. Stop {_px(stop)} ({r['stop_src']}) → "
                f"hisse başı risk {_px(per_share)} (%{tr_n(per_share / px * 100, 1)}). Riske göre en fazla {_q(_floor_qty(max_by_risk))} adet"
                + (f"; %{tr_n(maxpos, 0)} ağırlık sınırına göre en fazla {_q(_floor_qty(max_by_weight))} adet" if max_by_weight is not None else "")
                + ".")
        planned = q * per_share if q else None
        r["planned_risk"] = planned
        if not qty and not amount:
            st, tail = "ok", f" Adet girilmedi: önerilen {_q(suggested)} adet (≈{_usd(suggested * px)}, risk {_usd(r['suggested_risk'])})."
        else:
            ratio = planned / budget
            st = "ok" if ratio <= 1.05 else "warn" if ratio <= SIZE_STOP_RATIO else "stop"
            tail = (f" Plan {_q(q)} adet (≈{_usd(amt)}) → stopa kadar risk {_usd(planned)}, bütçenin {tr_n(ratio, 1)} katı"
                    f" (portföyün %{tr_n(planned / total * 100, 2)}).")
            r["risk_ratio"] = ratio
        r["checks"].append(_check("size", "Pozisyon büyüklüğü", st, head + tail,
                                  f"{r['portfolio']['src']}; ayar risk_per_trade_pct={tr_n(rpt, 1)}"))

    rk = None
    try:
        rk = risk.compute(led)
    except Exception:  # noqa: BLE001
        rk = None
    rows = (rk or {}).get("holdings") or []

    # 2. max position weight after the trade
    if post_total and amt:
        w_after = (existing + amt) / post_total * 100
        r["weight_after"] = w_after
        st = "ok" if w_after <= maxpos else "warn" if w_after <= maxpos * 1.5 else "stop"
        now = f"şu an %{tr_n(existing / total * 100, 1)}, " if existing else ""
        r["checks"].append(_check("weight", "Pozisyon ağırlığı", st,
                                  f"İşlem sonrası {sym} ağırlığı %{tr_n(w_after, 1)} ({now}sınır %{tr_n(maxpos, 0)}).",
                                  f"{r['portfolio']['src']}; ayar max_position_pct={tr_n(maxpos, 0)}"))
    else:
        r["checks"].append(_check("weight", "Pozisyon ağırlığı", "na", f"Ağırlık {NA} (fiyat, adet ya da portföy değeri eksik)."))

    # 3a. sector concentration
    sec = ind = None
    try:
        sec, ind = risk._sector(sym)
    except Exception:  # noqa: BLE001
        pass
    r["sector"], r["industry"] = sec, ind
    if not post_total or not amt:
        r["checks"].append(_check("sector", "Sektör yoğunluğu", "na", f"Sektör payı {NA} (adet ya da portföy değeri eksik)."))
    elif not sec or sec == "Bilinmiyor":
        r["checks"].append(_check("sector", "Sektör yoğunluğu", "na", f"{sym} sektörü {NA} (FMP/Yahoo profili yok)."))
    else:
        same = [h for h in rows if h["sector"] == sec and h["symbol"] != sym]
        sec_after = (sum(h["value"] for h in same) + existing + amt) / post_total * 100
        lim = risk.LIMITS["sector"]
        r["sector_after"] = sec_after
        names = ", ".join(h["symbol"] for h in sorted(same, key=lambda h: -h["value"])[:6])
        r["checks"].append(_check("sector", "Sektör yoğunluğu", "warn" if sec_after > lim else "ok",
                                  f"{sec} ({ind}) payı işlem sonrası %{tr_n(sec_after, 1)} (eşik %{tr_n(lim, 0)})"
                                  + (f"; aynı sektörde: {names}." if names else "; portföyde aynı sektörden başka hisse yok."),
                                  "FMP/Yahoo şirket profili; radar risk"))

    # 3b. correlation cluster
    held = [h["symbol"] for h in rows]
    corr, n_obs = ({}, 0)
    try:
        corr, n_obs = _corr_with_holdings(sym, held, (rk or {}).get("as_of") or r["portfolio"]["day"])
    except Exception:  # noqa: BLE001
        pass
    r["correlations"] = sorted(({"symbol": k, "corr": v} for k, v in corr.items()), key=lambda x: -x["corr"])
    r["corr_days"] = n_obs
    if not held:
        r["checks"].append(_check("cluster", "Korelasyon kümesi", "ok", "Portföyde başka pozisyon yok."))
    elif not corr or not post_total or not amt:
        why = f"{sym} için son {risk.WIN_CORR} işlem gününde yeterli ortak kapanış yok" if not corr else "adet ya da portföy değeri eksik"
        r["checks"].append(_check("cluster", "Korelasyon kümesi", "na", f"Korelasyon {NA}: {why}."))
    else:
        edge = risk.CORR_EDGE
        linked = {h for h, c in corr.items() if c >= edge}
        for cl in (rk or {}).get("clusters") or []:
            if linked & set(cl["symbols"]):
                linked |= set(cl["symbols"])
        linked.discard(sym)
        val = {h["symbol"]: h["value"] for h in rows}
        cl_after = (sum(val.get(h, 0) for h in linked) + existing + amt) / post_total * 100
        lim = risk.LIMITS["cluster"]
        r["cluster"] = {"symbols": sorted(linked, key=lambda h: -val.get(h, 0)), "weight_after": cl_after}
        top = ", ".join(f"{x['symbol']} {tr_n(x['corr'], 2)}" for x in r["correlations"][:4])
        if not linked:
            r["checks"].append(_check("cluster", "Korelasyon kümesi", "ok",
                                      f"{sym} hiçbir pozisyonla {tr_n(edge, 2)} üstünde korelasyonlu değil (en yüksek: {top}).",
                                      f"Yahoo günlük kapanış, {n_obs} ortak gün"))
        else:
            r["checks"].append(_check("cluster", "Korelasyon kümesi", "warn" if cl_after > lim else "ok",
                                      f"{sym} birlikte hareket eden kümeye katılıyor ({', '.join(r['cluster']['symbols'][:6])}); "
                                      f"küme işlem sonrası %{tr_n(cl_after, 1)} (eşik %{tr_n(lim, 0)}). En yüksek korelasyonlar: {top}.",
                                      f"Yahoo günlük kapanış, {n_obs} ortak gün; eşik korelasyon {tr_n(edge, 2)}"))

    # 4. regime ceiling
    try:
        from . import regime
        reg = regime.latest()
    except Exception:  # noqa: BLE001
        reg = None
    ceil = risk.ceiling(reg["overall"]) if reg else None
    if ceil is None or not post_total or not amt:
        r["checks"].append(_check("regime", "Rejim tavanı", "na", f"Rejim tavanı {NA} (rejim kaydı ya da adet eksik)."))
    else:
        stock_now = sum(h["value"] for h in rows)
        before = stock_now / total * 100
        after = (stock_now + amt) / post_total * 100
        r["regime"] = {"label": reg["label"], "score": reg["overall"], "day": reg["day"], "ceiling": ceil, "stock_after": after}
        st = "warn" if after > ceil + 0.5 else "ok"
        extra = f" Zaten tavanın üstündesin (%{tr_n(before, 0)}); bu alım farkı büyütür." if before > ceil + 0.5 else ""
        r["checks"].append(_check("regime", "Rejim tavanı", st,
                                  f"Hisse oranı işlem sonrası %{tr_n(after, 0)}; {reg['label']} rejiminde ({tr_n(reg['overall'], 0)} puan) "
                                  f"kural tavanı %{ceil}.{extra}", f"radar rejim {_d(reg['day'])}"))

    # 5. circuit breaker, 6. revenge
    _circuit(r, led, total, today)

    # 7. plan / thesis
    _plan_buy(r, sym)

    # 8. earnings
    e = _earnings(sym, today)
    r["earnings"] = e
    if not e:
        r["checks"].append(_check("earnings", "Bilanço tarihi", "na", f"Sonraki bilanço tarihi {NA} (radar takvimi ve Yahoo'da yok)."))
    else:
        n = _tdays(today, e["day"])
        e["tdays"] = n
        when = _d(e["day"]) + (f" {e['time']} NY" if e.get("time") else "")
        if n <= EARN_DAYS:
            r["checks"].append(_check("earnings", "Bilanço tarihi", "warn",
                                      f"Bilanço {when} ({'bugün' if n == 0 else f'{n} işlem günü sonra'}): {e['name']}. "
                                      "Bilanço öncesi girişte gap riski stopu atlayabilir; boyutu küçültmeyi ya da sonrasını beklemeyi düşün.",
                                      e["source"]))
        else:
            r["checks"].append(_check("earnings", "Bilanço tarihi", "ok", f"Sonraki bilanço {when} ({n} işlem günü sonra).", e["source"]))


def _setting_pct(key: str, default: float) -> float:
    try:
        v = float(str(db.get_setting(key)).replace(",", "."))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _circuit(r: dict, led, total: float | None, today: date) -> None:
    closed = portfolio.closed_positions(led) if led.days else []
    closed = [c for c in closed if c.get("close")]
    closed.sort(key=lambda c: c["close"], reverse=True)
    r["recent_trips"] = [{"symbol": c["symbol"], "open": c["open"], "close": c["close"], "realized": c["realized"],
                          "pct": c.get("pct"), "days": c.get("days")} for c in closed[:5]]
    if not total:
        r["checks"].append(_check("breaker", "Devre kesici", "na", f"Portföy değeri {NA}."))
    else:
        parts, st = [], "ok"
        last2 = closed[:2]
        streak = (len(last2) == 2 and all(c["realized"] < 0 for c in last2)
                  and all((today - date.fromisoformat(c["close"])).days <= STREAK_DAYS for c in last2))
        if streak:
            st = "stop"
            parts.append("Soğuma: son iki kapanan pozisyon zararla ve son 5 gün içinde kapandı ("
                         + "; ".join(f"{c['symbol']} {_d(c['close'])} {_usd(c['realized'])}" for c in last2)
                         + "). Yeni pozisyon açmadan önce bu iki işlemi gözden geçir.")
        wk, wn = _realized_since(led, (today - timedelta(days=7)).isoformat())
        mo, mn = _realized_since(led, today.replace(day=1).isoformat())
        wk_pct, mo_pct = -wk / total * 100, -mo / total * 100
        r["realized_7d"], r["realized_mtd"] = wk, mo
        if wk_pct > WEEK_LOSS_PCT:
            st = "stop"
            parts.append(f"Son 7 günde gerçekleşen zarar {_usd(wk)} = portföyün %{tr_n(wk_pct, 1)}'i (sınır %{tr_n(WEEK_LOSS_PCT, 0)}).")
        if mo_pct > MONTH_LOSS_PCT:
            st = "stop"
            parts.append(f"Ay başından beri gerçekleşen zarar {_usd(mo)} = portföyün %{tr_n(mo_pct, 1)}'i (sınır %{tr_n(MONTH_LOSS_PCT, 0)}).")
        if st == "ok":
            parts.append(f"Son 7 gün gerçekleşen K/Z {_usd(wk)} ({wn} satış, sınır −%{tr_n(WEEK_LOSS_PCT, 0)}); "
                         f"ay başından {_usd(mo)} ({mn} satış, sınır −%{tr_n(MONTH_LOSS_PCT, 0)}); art arda iki zararlı kapanış yok.")
        r["checks"].append(_check("breaker", "Devre kesici", st, " ".join(parts),
                                  f"radar defteri (tüm satışlar, NY günü), {r['today']} itibarıyla"))

    losers = [c for c in closed if c["realized"] < 0 and 0 <= _tdays(c["close"], today) <= REVENGE_DAYS]
    if losers:
        same = [c for c in losers if c["symbol"] == r["symbol"]]
        txt = "; ".join(f"{c['symbol']} {_d(c['close'])} {_usd(c['realized'])} ({_pc(c['pct'])})" for c in losers[:3])
        r["checks"].append(_check("revenge", "İntikam riski", "warn",
                                  f"Son {REVENGE_DAYS} işlem gününde zararla kapanan pozisyon var: {txt}."
                                  + (f" Aynı hisseye ({r['symbol']}) hemen geri dönüyorsun." if same else "")
                                  + " Kayıp sonrası hızlı giriş, planı atlamanın en sık yolu; girişin plandan geldiğinden emin ol.",
                                  "radar defteri, kapanan pozisyonlar"))
    else:
        r["checks"].append(_check("revenge", "İntikam riski", "ok", f"Son {REVENGE_DAYS} işlem gününde zararla kapanan pozisyon yok.",
                                  "radar defteri, kapanan pozisyonlar"))


def _plan_buy(r: dict, sym: str) -> None:
    from . import thesis
    t = None
    try:
        t = thesis.for_symbol(sym)
    except Exception:  # noqa: BLE001
        pass
    link = f"/tez?symbol={sym}"
    r["thesis"] = _thesis_brief(t)
    if not t:
        r["checks"].append(_check("plan", "Plan (tez)", "warn", "Plansız giriş: önce tez + çıkış kuralı yaz.", "tez defteri", link=link))
        return
    exits = [x for x in t["rules"] if not x["good"]]
    trig = [x for x in t["rules"] if x["status"] == "triggered"]
    if trig:
        r["checks"].append(_check("plan", "Plan (tez)", "warn",
                                  f"Tez ({t['status_tr']}) var ama kuralı tetiklenmiş: {'; '.join(x['label'] for x in trig)}. "
                                  "Ekleme yapmadan önce tezi gözden geçir.", "tez defteri", link=f"/tez#t{t['id']}"))
    elif not exits:
        r["checks"].append(_check("plan", "Plan (tez)", "warn",
                                  f"Tez ({t['status_tr']}) var ama çıkış (zarar kes) kuralı yok. Bir çıkış kuralı ekle.",
                                  "tez defteri", link=f"/tez#t{t['id']}"))
    else:
        r["checks"].append(_check("plan", "Plan (tez)", "ok",
                                  f"Tez ({t['status_tr']}{', ' + t['kind'] if t.get('kind') else ''}) ve {len(exits)} çıkış kuralı var: "
                                  f"{'; '.join(x['label'] for x in exits[:3])}.", "tez defteri", link=f"/tez#t{t['id']}"))


def _thesis_brief(t: dict | None) -> dict | None:
    if not t:
        return None
    return {"id": t["id"], "status": t["status_tr"], "kind": t.get("kind"), "text": t.get("thesis"),
            "rules": [{"label": x["label"], "status": x["status"], "good": x["good"]} for x in t["rules"]]}


def _sell(r: dict, led, hold: dict | None, qty, amount, today: date) -> None:
    sym, px = r["symbol"], r["price"]
    if not hold:
        r["qty"] = qty
        r["checks"].append(_check("position", "Pozisyon", "stop", f"{sym} portföyde yok; satılacak pozisyon bulunamadı.", "radar defteri"))
        _plan_sell(r, None, None, None)
        return
    if qty:
        q, src = float(qty), "girilen adet"
    elif amount and px:
        q, src = amount / px, f"{_usd(amount)} tutar ÷ {_px(px)}"
    else:
        q, src = hold["qty"], "pozisyonun tamamı"
    r["qty"], r["qty_src"] = q, src
    held_txt = f"Elde {_q(hold['qty'])} adet, ortalama maliyet {_px(hold['avg'])}" + (f" ({', '.join(hold['subs'])})" if hold["subs"] else "")
    if q > hold["qty"] + 1e-6:
        r["checks"].append(_check("position", "Pozisyon", "stop", f"{held_txt}; {_q(q)} adet satılamaz.", r["portfolio"]["src"]))
        q = hold["qty"]
    else:
        part = "tamamı" if q >= hold["qty"] - 1e-6 else f"%{tr_n(q / hold['qty'] * 100, 0)}'i"
        r["checks"].append(_check("position", "Pozisyon", "ok", f"{held_txt}; satış pozisyonun {part}.", r["portfolio"]["src"]))

    est = pct = None
    if px and hold["avg"]:
        est = (px - hold["avg"]) * q
        pct = (px / hold["avg"] - 1) * 100
        r["amount"] = q * px
        r["est_realized"], r["est_pct"] = est, pct
        r["checks"].append(_check("pnl", "Tahmini gerçekleşen K/Z", "ok",
                                  f"{_q(q)} × ({_px(px)} − {_px(hold['avg'])}) ≈ {_usd(est)} ({_pc(pct)}); satış tutarı ≈{_usd(q * px)}. "
                                  "Komisyon ve kur farkı hariç.", r["price_src"]))
    else:
        r["checks"].append(_check("pnl", "Tahmini gerçekleşen K/Z", "na", f"K/Z {NA}: fiyat yok."))

    days = tdays = None
    if hold["first_day"]:
        days = (today - date.fromisoformat(hold["first_day"])).days
        tdays = _tdays(hold["first_day"], today)
        r["hold_days"], r["hold_tdays"] = days, tdays
        r["checks"].append(_check("hold", "Tutma süresi", "ok",
                                  f"İlk alım {_d(hold['first_day'])}: {days} takvim günü (~{tdays} işlem günü)"
                                  + (" — 1 yıldan kısa." if days < 365 else "."), "radar defteri"))
    _plan_sell(r, pct, tdays, hold)

    e = _earnings(sym, today)
    r["earnings"] = e
    if e:
        n = _tdays(today, e["day"])
        e["tdays"] = n
        if n <= EARN_DAYS:
            r["notes"].append(f"Bilanço {_d(e['day'])} ({n} işlem günü sonra; {e['source']}). Satış bilanço öncesi riski kaldırır; "
                              "kararın bilanço beklentisinden mi plandan mı geldiğini not et.")


def _plan_sell(r: dict, pct: float | None, tdays: int | None, hold: dict | None) -> None:
    from . import thesis
    sym = r["symbol"]
    t = None
    try:
        t = thesis.for_symbol(sym)
    except Exception:  # noqa: BLE001
        pass
    r["thesis"] = _thesis_brief(t)
    if not hold:
        return
    trig = [x for x in (t["rules"] if t else []) if x["status"] == "triggered"]
    good = [x for x in trig if x["good"]]
    bad = [x for x in trig if not x["good"]]
    rules_txt = ("; ".join(f"{x['label']} → {'TETİKLENDİ' if x['status'] == 'triggered' else 'bekliyor'}" for x in t["rules"][:4])
                 if t and t["rules"] else "")
    link = f"/tez#t{t['id']}" if t else f"/tez?symbol={sym}"
    if not t:
        r["checks"].append(_check("plan", "Plan (tez)", "ok", f"{sym} için tez yok. Satışın nedenini tez defterine bir satırla not et.",
                                  "tez defteri", link=link))
    elif pct is not None and pct > 0 and tdays is not None and tdays < SHORT_HOLD_DAYS and not good:
        r["checks"].append(_check("plan", "Plan (tez)", "warn",
                                  f"Kazananı kısa sürede satıyorsun ({tdays} işlem günü, {_pc(pct)}) ve hedef kuralı tetiklenmedi. "
                                  f"Kurallar: {rules_txt or 'yok'}.", "tez defteri", link=link))
    elif pct is not None and pct < 0 and bad:
        r["checks"].append(_check("plan", "Plan (tez)", "ok",
                                  f"Zarar kes kuralı tetiklendi: {'; '.join(x['label'] for x in bad)}. Satış plana uygun.",
                                  "tez defteri", link=link))
    elif good:
        r["checks"].append(_check("plan", "Plan (tez)", "ok", f"Hedef kuralı tetiklendi: {'; '.join(x['label'] for x in good)}.",
                                  "tez defteri", link=link))
    else:
        r["checks"].append(_check("plan", "Plan (tez)", "ok",
                                  f"Tez ({t['status_tr']}) kuralları: {rules_txt or 'kural yok'}. Hiçbir kural tetiklenmedi; "
                                  "satışın nedenini tez notu olarak yaz.", "tez defteri", link=link))


def _summary(r: dict) -> str:
    q = r.get("qty")
    amt = r.get("amount")
    head = f"{r['symbol']} {_q(q)} adet {r['side_tr'].lower()}" + (f" (≈{_usd(amt)})" if amt else "")
    issues = [_lower(c["title"]) for c in r["checks"] if c["status"] in ("warn", "stop")]
    parts = [head + (": " + ", ".join(issues) + "." if issues else ": tüm kontroller uygun.")]
    if r["side"] == "buy" and r.get("suggested_qty") is not None and r.get("per_share_risk"):
        parts.append(f"Önerilen en fazla {_q(r['suggested_qty'])} adet (risk {_usd(r.get('suggested_risk'))}).")
    if r["side"] == "sell" and r.get("est_realized") is not None:
        parts.append(f"Tahmini gerçekleşen {_usd(r['est_realized'])} ({_pc(r['est_pct'])}).")
    return " ".join(parts)


def brief(r: dict | None) -> dict | None:
    """Compact, JSON-friendly version for the CLI / Hermes."""
    if not r:
        return None
    rd = lambda v, d=2: None if v is None else round(v, d)  # noqa: E731
    out = {"verdict": r["verdict"], "summary": r["summary"], "side": r["side"], "symbol": r["symbol"], "today": r["today"],
           "price": rd(r["price"]), "price_src": r["price_src"], "qty": rd(r.get("qty"), 4), "qty_src": r.get("qty_src"),
           "amount_usd": rd(r.get("amount"), 0),
           "portfolio_value_usd": rd(r["portfolio"]["value"], 0), "portfolio_day": r["portfolio"]["day"],
           "checks": [{"key": c["key"], "title": c["title"], "status": c["status"], "reason": c["reason"], "source": c["source"]}
                      for c in r["checks"]],
           "notes": r["notes"]}
    if r["side"] == "buy":
        out.update({"stop": rd(r.get("stop")), "stop_src": r.get("stop_src"), "risk_budget_usd": rd(r.get("budget"), 0),
                    "per_share_risk": rd(r.get("per_share_risk")), "suggested_qty": r.get("suggested_qty"),
                    "suggested_risk_usd": rd(r.get("suggested_risk"), 0), "planned_risk_usd": rd(r.get("planned_risk"), 0),
                    "weight_after_pct": rd(r.get("weight_after"), 1), "sector": r.get("sector"),
                    "sector_after_pct": rd(r.get("sector_after"), 1),
                    "top_correlations": [{"symbol": x["symbol"], "corr": rd(x["corr"])} for x in (r.get("correlations") or [])[:5]]})
    else:
        out.update({"est_realized_usd": rd(r.get("est_realized"), 0), "est_pct": rd(r.get("est_pct"), 1),
                    "hold_days": r.get("hold_days")})
    ctx = r.get("context") or {}
    if ctx.get("karne"):
        out["karne"] = ctx["karne"]
    if ctx.get("setup"):
        out["setup"] = {k: (rd(v) if isinstance(v, float) else v) for k, v in ctx["setup"].items()}
    if r.get("earnings"):
        out["earnings"] = r["earnings"]
    return out
