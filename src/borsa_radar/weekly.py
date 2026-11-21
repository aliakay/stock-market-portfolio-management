"""Sunday "Haftaya bakış": template-only weekly Telegram digest (no LLM).

Last week: portfolio vs S&P 500, best/worst holdings, regime now vs a week
ago, setups decided, price alerts fired. Next week: theses to review,
holdings' earnings with options-implied moves, important macro releases.
Every number comes from the radar's own tables; missing → "doğrulanamadı".
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import config, db, notifier
from .notifier import esc
from .util import iso, to_local, tr_n, utcnow

log = logging.getLogger(__name__)

KIND = "weekly"
MAX_CHARS = 3500
NA = "doğrulanamadı"
DAYS3 = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def _pct(v, d: int = 1) -> str:
    return NA if v is None else "%" + tr_n(v, d, sign=True)


def _usd(v, sign: bool = True) -> str:
    if v is None:
        return NA
    s = tr_n(abs(v), 0)
    return (("+" if v >= 0 else "−") if sign else ("−" if v < 0 else "")) + s + "$"


def _dm(day: str) -> str:
    d = date.fromisoformat(day[:10])
    return f"{DAYS3[d.weekday()]} {d:%d.%m}"


def _safe(label: str, fn, default=None):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly %s failed: %s", label, exc)
        return default


# ── sections (each returns a list of lines; first line is the heading) ──

def _portfolio() -> tuple[list[str], dict]:
    from . import portfolio, prices
    perf = portfolio.total_perf()
    w = (perf.get("periods") or {}).get("week")
    if not w:
        return ["<b>Geçen hafta</b>", f"Portföy performansı {NA}."], {}
    as_of = perf["as_of"]
    spx = (w.get("bench") or {}).get("SPX")
    lines = [f"<b>Geçen hafta</b> ({esc(_dm(w['from']))} → {esc(_dm(as_of))} kapanışı)"]
    diff = w["pct"] - spx if w.get("pct") is not None and spx is not None else None
    verdict = "" if diff is None else (f" → endeksin {tr_n(abs(diff), 1)} puan {'önünde' if diff >= 0 else 'gerisinde'}")
    lines.append(f"Portföy {_pct(w.get('pct'))} ({_usd(w.get('pnl'))}) · S&amp;P 500 {_pct(spx)}{verdict}")
    moves = []
    for p in portfolio.positions():
        if p["asset"] not in ("stock", "etf"):
            continue
        s = prices.series(p["symbol"], (date.fromisoformat(w["from"]) - timedelta(days=10)).isoformat())
        c0, c1 = s.at(w["from"]), s.at(as_of)
        if c0 and c1:
            moves.append((p["symbol"], (c1 / c0 - 1) * 100, p["qty"] * p["multiplier"] * (c1 - c0)))
    if moves:
        moves.sort(key=lambda m: m[1], reverse=True)
        fmt = lambda m: f"{esc(m[0])} {_pct(m[1])} ({_usd(m[2])})"  # noqa: E731
        best = [fmt(m) for m in moves[:3] if m[1] > 0]
        worst = [fmt(m) for m in reversed(moves[-3:]) if m[1] < 0]
        if best:
            lines.append("En iyi: " + ", ".join(best))
        if worst:
            lines.append("En kötü: " + ", ".join(worst))
    else:
        lines.append(f"Pozisyon bazında haftalık değişim {NA}.")
    return lines, {"from": w["from"], "as_of": as_of}


def _regime() -> list[str]:
    from . import regime
    hist = regime.history((date.today() - timedelta(days=30)).isoformat())
    if not hist:
        return [f"Rejim {NA}."]
    now = hist[-1]
    ref_day = (date.fromisoformat(now["day"]) - timedelta(days=7)).isoformat()
    then = next((r for r in reversed(hist) if r["day"] <= ref_day), None)
    s = f"Rejim: <b>{esc(now['label'])}</b> {tr_n(now['overall'], 0)} ({esc(_dm(now['day']))})"
    if then:
        d = now["overall"] - then["overall"] if now["overall"] is not None and then["overall"] is not None else None
        same = then["label"] == now["label"]
        s += (f" · bir hafta önce {esc(then['label'])} {tr_n(then['overall'], 0)}"
              + (f" ({tr_n(d, 0, sign=True)} puan{'' if same else ', etiket değişti'})" if d is not None else ""))
    else:
        s += f" · bir hafta önceki değer {NA}"
    return [s]


def _setups(win: dict) -> list[str]:
    from . import setups
    since = win.get("from") or (date.today() - timedelta(days=7)).isoformat()
    rows = [setups.load(r) for r in db.query(
        "SELECT * FROM setups WHERE source = 'live' AND exit_day IS NOT NULL AND exit_day > ? ORDER BY exit_day", (since,))]
    tr = setups.track_record()["all"]
    rec = (f"genel isabet %{tr_n(tr['hit'], 0)} ({tr['hits']}/{tr['decided']}), ort. {tr_n(tr['avg_r'], 2, sign=True)}R"
           if tr.get("decided") else f"henüz sonuçlanan setup yok ({tr.get('open', 0)} açık)")
    if not rows:
        return [f"Setup: bu hafta sonuçlanan yok · {rec}"]
    lines = [f"Setup: bu hafta {len(rows)} sonuçlandı · {rec}"]
    for r in rows[:5]:
        rm = f" {tr_n(r['r_mult'], 2, sign=True)}R" if r.get("r_mult") is not None else ""
        lines.append(f"• {esc(r['symbol'])} {esc(r['name_tr'])} → {esc(r['status_tr'])}{rm} ({esc(_dm(r['exit_day']))})")
    return lines


def _alerts() -> list[str]:
    since = iso(utcnow() - timedelta(days=7))
    rows = [dict(r) for r in db.query("SELECT * FROM price_alerts WHERE fired_at IS NOT NULL AND fired_at >= ? ORDER BY fired_at", (since,))]
    if not rows:
        return []
    try:
        from .alerts import label
    except Exception:  # noqa: BLE001
        label = None
    lines = [f"Fiyat alarmları: {len(rows)} tetiklendi"]
    for r in rows[:5]:
        txt = label(r) if label else f"{r['symbol']} {r['cond']} {tr_n(r['level'])}"
        val = f" (değer {tr_n(r['fired_value'])})" if r.get("fired_value") is not None else ""
        lines.append(f"• {esc(txt)}{val} · {esc(to_local(r['fired_at']).strftime('%d.%m'))}")
    return lines


def _theses() -> list[str]:
    from . import thesis
    horizon = (date.today() + timedelta(days=7)).isoformat()
    due = []
    for t in thesis.all_(include_closed=False):
        why = []
        if t["triggered"]:
            why.append("kural tetiklendi: " + ", ".join(r["label"] for r in t["triggered"][:2]))
        if t["review_due"]:
            why.append("gözden geçirme zamanı geçti")
        elif t["next_review"] and t["next_review"] <= horizon:
            why.append(f"gözden geçirme {_dm(t['next_review'])}")
        if why:
            due.append(f"• {esc(t['symbol'])}: {esc('; '.join(why))}")
    if not due:
        return []
    return [f"<b>Tezler</b> ({len(due)})"] + due[:6]


def _earnings() -> list[str]:
    from . import earnings
    u = earnings.upcoming(7, quick=True)
    held = [b for b in u["soon"] if b["held"]]
    watch = [b for b in u["soon"] if not b["held"]]
    lines = ["<b>Bu hafta bilançolar</b>"]
    if not held and not watch:
        return lines + ["Portföy ve izleme listesinde bilanço yok."]
    for b in held:
        live = _safe(f"earnings {b['symbol']}", lambda s=b["symbol"]: earnings.build(s), None) or b  # fresh chain for holdings
        n, im, st, ex = live["next"] or b["next"], live["implied"], live["stats"], live["exposure"]
        parts = [f"• <b>{esc(b['symbol'])}</b> {esc(_dm(n['date']))} {esc(n['timing_tr'])}"]
        eps = live["est"]["eps"] if live["est"]["eps"] is not None else n.get("eps")
        if eps is not None:
            parts.append(f"EPS bekl. {tr_n(eps)}$")
        if im:
            risk = f", pozisyonda ±{tr_n(ex['risk_implied'], 0)}$" if ex.get("risk_implied") is not None else ""
            parts.append(f"opsiyon ±%{tr_n(im['pct'], 1)}{risk}")
        else:
            parts.append(f"opsiyon hareketi {NA}")
        if st["n_moves"]:
            parts.append(f"geçmiş ort. ±%{tr_n(st['avg_abs'], 1)}")
        lines.append(" · ".join(parts))
    if watch:
        lines.append("İzleme listesi: " + ", ".join(f"{esc(b['symbol'])} {esc(_dm(b['next']['date']))}" for b in watch[:8]))
    return lines


def _macro() -> list[str]:
    from .collectors import macro
    rows = [r for r in macro.upcoming(24 * 7) if (r["importance"] or 0) >= 3 and not str(r["key"]).startswith("earnings:")]
    if not rows:
        return []
    lines = ["<b>Makro takvim</b>"]
    for r in rows[:8]:
        loc = to_local(r["release_at"])
        star = "🔥" if r["importance"] >= 5 else "⭐" if r["importance"] >= 4 else "·"
        lines.append(f"{star} {DAYS3[loc.weekday()]} {loc:%d.%m %H:%M} {esc(r['name'])}")
    if len(rows) > 8:
        lines.append(f"… ve {len(rows) - 8} veri daha")
    return lines


# ── assemble ──────────────────────────────────────────────

def build() -> str:
    today = datetime.now(config.LOCAL_TZ)
    pf, win = _safe("portfolio", _portfolio, (["<b>Geçen hafta</b>", f"Portföy {NA}."], {}))
    blocks = [
        pf + _safe("regime", _regime, [f"Rejim {NA}."]) + _safe("setups", lambda: _setups(win), [f"Setup {NA}."])
        + _safe("alerts", _alerts, []),
        _safe("theses", _theses, []),
        _safe("earnings", _earnings, ["<b>Bu hafta bilançolar</b>", NA]),
        _safe("macro", _macro, []),
    ]
    head = f"<b>🗓 Haftaya bakış · {today:%d.%m.%Y}</b>"
    foot = f"🔗 {config.PUBLIC_BASE_URL}/bilancolar · {config.PUBLIC_BASE_URL}/portfolio"

    def render(bs):
        return "\n\n".join([head] + ["\n".join(b) for b in bs if b] + [foot])

    text = render(blocks)
    # keep under the limit: trim the longest lists first (macro, earnings watch lines, setups detail)
    order = [3, 2, 1, 0]
    while len(text) > MAX_CHARS:
        for i in order:
            if len(blocks[i]) > 2:
                blocks[i] = blocks[i][:-1]
                break
        else:
            text = text[:MAX_CHARS - 1] + "…"
            break
        text = render(blocks)
    return text


def send(force: bool = False) -> bool:
    day = datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d")
    if not force and db.query_one("SELECT 1 FROM digests WHERE kind = ? AND day = ?", (KIND, day)):
        return False
    text = build()
    db.execute("INSERT INTO digests(kind, day, content, created_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT(kind, day) DO UPDATE SET content = excluded.content, created_at = excluded.created_at",
               (KIND, day, text, iso(utcnow())))
    return notifier.send_now(text, kind="digest")
