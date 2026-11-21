"""Command line interface: `borsa-radar ...` (also used by the Hermes skill)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta

from . import config, db, tickers
from .util import fmt_local, iso, utcnow


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif isinstance(obj, list):
        for x in obj:
            print(x)
    else:
        print(obj)


def cmd_ticker(a) -> int:
    try:
        if a.action == "add":
            for s in a.symbols:
                r = tickers.add(s, notes=a.notes)
                msg = {"added": "eklendi", "exists": "zaten listede", "reactivated": "yeniden aktif"}[r["status"]]
                print(f"✓ {r['symbol']} ({r.get('name')}) {msg}")
            if a.symbols:
                print("İlk taramada geçmiş haberler sessizce yüklenir; yeni gelişmeler bildirilecek.")
        elif a.action in ("remove", "rm"):
            for s in a.symbols:
                r = tickers.remove(s, purge=a.purge)
                print(f"✓ {r['symbol']} {'silindi' if r['status'] == 'deleted' else 'takipten çıkarıldı (geçmiş korunur)'}")
        elif a.action in ("list", "ls"):
            rows = db.all_tickers()
            if a.json:
                _print([dict(r) for r in rows], True)
            else:
                for r in rows:
                    flag = "✓" if r["active"] else "✗"
                    th = f" eşik={r['alert_threshold']}" if r["alert_threshold"] else ""
                    print(f"{flag} {r['symbol']:<7} {r['name'] or ''}{th}")
        elif a.action == "set":
            r = tickers.set_options(a.symbols[0], threshold=a.threshold, notes=a.notes, clear_threshold=a.clear_threshold)
            print(f"✓ {r['symbol']} güncellendi (eşik={r['alert_threshold'] or 'varsayılan'})")
    except tickers.TickerError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_events(a) -> int:
    where = ["e.published_at >= ?", "e.status IN ('analyzed','backfill','pending')"]
    params: list = [iso(utcnow() - timedelta(days=a.days))]
    if a.symbol:
        where.append("EXISTS (SELECT 1 FROM event_symbols s WHERE s.event_id = e.id AND s.symbol = ?)")
        params.append(a.symbol.upper())
    if a.type:
        where.append("e.type = ?")
        params.append(a.type)
    if a.min_importance:
        where.append("COALESCE(e.importance, 0) >= ?")
        params.append(a.min_importance)
    rows = db.query(
        f"""SELECT e.id, e.type, e.symbol, e.title, e.published_at, e.importance, e.direction, e.url, e.source,
                   a.summary_tr, a.reasoning_tr
            FROM events e LEFT JOIN analyses a ON a.event_id = e.id
            WHERE {' AND '.join(where)} ORDER BY e.published_at DESC LIMIT ?""",
        params + [a.limit],
    )
    if a.json:
        _print([dict(r) | {"symbols": db.event_symbols(r["id"]), "link": f"{config.PUBLIC_BASE_URL}/e/{r['id']}"} for r in rows], True)
        return 0
    d = {1: "+", -1: "-", 0: "=", None: " "}
    for r in rows:
        print(f"#{r['id']:<6} {fmt_local(r['published_at'])}  [{r['type']:<6}] {d[r['direction']]} önem {r['importance'] or '-'}  {r['title']}")
        if r["summary_tr"]:
            print(f"         {r['summary_tr']}")
    return 0


def cmd_event(a) -> int:
    e = db.get_event(a.id)
    if not e:
        print("bulunamadı", file=sys.stderr)
        return 1
    out = dict(e) | {
        "symbols": db.event_symbols(a.id),
        "extra": db.loads(e["extra"], {}),
        "portfolio_impact": db.loads(e["portfolio_impact"], None),
        "price_reactions": [dict(r) for r in db.query("SELECT * FROM price_reactions WHERE event_id = ?", (a.id,))],
        "link": f"{config.PUBLIC_BASE_URL}/e/{a.id}",
    }
    _print(out, True)
    return 0


def cmd_calendar(a) -> int:
    from .collectors import macro
    rows = macro.upcoming(a.days * 24)
    if a.json:
        _print([dict(r) for r in rows], True)
        return 0
    for r in rows:
        print(f"{fmt_local(r['release_at'], '%a %d.%m %H:%M')}  önem {r['importance']}  {r['name']}")
    return 0


def cmd_status(a) -> int:
    from . import llm
    jobs = db.query("SELECT * FROM job_runs ORDER BY job")
    counts = db.query("SELECT type, status, COUNT(*) n FROM events GROUP BY type, status ORDER BY type, status")
    queued = db.query_one("SELECT COUNT(*) n FROM notifications WHERE status = 'queued'")["n"]
    out = {
        "tickers": [r["symbol"] for r in db.active_tickers()],
        "events": [dict(r) for r in counts],
        "notifications_queued": queued,
        "llm_calls_today": llm.usage_today(),
        "jobs": [dict(r) for r in jobs],
    }
    _print(out, True)
    return 0


def cmd_run(a) -> int:
    from . import pipeline
    pipeline.run_all_once()
    print("✓ tam döngü tamamlandı")
    return 0


def cmd_digest(a) -> int:
    from . import digest
    if a.send:
        print("gönderildi" if digest.send(a.kind, force=True) else "gönderilemedi")
    else:
        print(digest.build(a.kind))
    return 0


def cmd_analyze(a) -> int:
    from . import analyzer
    print("✓" if analyzer.analyze_one(a.id) else "✗")
    return 0


def cmd_research(a) -> int:
    from . import research
    research.request(a.id)
    if a.now:
        research.run_one()
        print(json.dumps(research.get(a.id), ensure_ascii=False, indent=2, default=str))
    else:
        print("✓ kuyruğa alındı (~2 dk içinde çalışır)")
    return 0


def cmd_fundamentals(a) -> int:
    from .collectors import xbrl
    rows = xbrl.quarter_table(a.symbol.upper(), a.n)
    if a.json:
        _print(rows, True)
        return 0
    for r in rows:
        lbl = "Q4" if r["fp"] == "FY" else r["fp"]
        yoy = f"{r['rev_yoy']:+.0f}%" if r["rev_yoy"] is not None else "—"
        dil = f"{r['shares_qoq']:+.1f}%" if r["shares_qoq"] is not None else "—"
        print(f"{r['period_end']} {lbl:<3} gelir {xbrl._fmt_usd(r['revenue']):>10} ({yoy:>6})  net {xbrl._fmt_usd(r['net_income']):>10}  "
              f"EPS {r['eps_diluted'] if r['eps_diluted'] is not None else '—':>6}  hisse Δ {dil}")
    return 0


def cmd_odds(a) -> int:
    from .collectors import kalshi
    print(kalshi.context_text() or "veri yok")
    return 0


def cmd_positioning(a) -> int:
    from .collectors import positioning
    _print({"short_volume": positioning.short_volume_series(a.symbol.upper(), 20),
            "short_interest": positioning.short_interest_series(a.symbol.upper(), 6),
            "reddit": positioning.social_series(a.symbol.upper(), 14)}, True)
    return 0


def _usd(v) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def cmd_portfolio(a) -> int:
    from . import portfolio
    led = portfolio.build(force=True)
    perf = portfolio.total_perf(led)
    if a.json:
        _print({"perf": perf, "allocation": portfolio.allocation(led), "positions": portfolio.positions(led=led),
                "warnings": led.warnings}, True)
        return 0
    if not perf.get("as_of"):
        print("Portföy boş. `borsa-radar import revolut DOSYA.csv --sub P1` ya da `borsa-radar trade add ...`")
        return 0
    print(f"Toplam {_usd(perf['value'])}  (nakit {_usd(perf['cash'])}) · fiyat günü {perf['as_of']}")
    for key, label in portfolio.PERIODS:
        p = perf["periods"][key]
        b = " · ".join(f"{k} {_pct(v)}" for k, v in p["bench"].items())
        print(f"  {label:<22} {_pct(p['pct']):>9}  {_usd(p['pnl']):>12}   {b}")
    if perf.get("cagr") is not None:
        print(f"  Yıllık ort. (CAGR)     {_pct(perf['cagr']):>9}")
    print()
    for al in portfolio.allocation(led):
        s = al["sub"]
        print(f"{s['code']} {s['name']:<12} {al['n']:>3} poz  {_usd(al['positions']):>12}  nakit {_usd(al['cash']):>10}  "
              f"pay {al['share'] or 0:5.1f}%  gün {_pct(al['perf']['periods'].get('day', {}).get('pct'))}")
    print()
    for p in portfolio.positions(led=led)[: a.top]:
        print(f"  {p['sub']:<3} {p['symbol']:<8} {p['qty']:>10.4g} @ {p['avg']:>9.2f}  fiyat {p['price'] or 0:>9.2f}  "
              f"{_usd(p['value']):>11}  K/Z {_pct(p['unreal_pct'])}{' (eski fiyat)' if p['stale'] else ''}")
    for w in led.warnings:
        print(f"⚠ {w}")
    return 0


def cmd_trade(a) -> int:
    from . import portfolio
    try:
        if a.action == "add":
            side, sym, qty, price = a.args[:4]
            tid = portfolio.add_trade(side=side, symbol=sym, qty=float(qty), price=float(price), sub=a.sub, ts=a.date,
                                      fees=a.fees, currency=a.currency, note=a.note, asset=a.asset)
            portfolio.refresh_prices()
            print(f"✓ işlem #{tid} eklendi")
        elif a.action in ("rm", "remove"):
            portfolio.delete_trade(int(a.args[0]))
            print("✓ silindi")
        elif a.action == "move":
            n = portfolio.move_symbol(a.args[0], a.args[1])
            print(f"✓ {n} işlem {a.args[1]}'e taşındı")
        elif a.action in ("list", "ls"):
            rows, n = portfolio.trades(symbol=a.symbol, limit=a.limit)
            if a.json:
                _print(rows, True)
                return 0
            for r in rows:
                rz = f" realize {_usd(r.get('realized'))}" if r.get("realized") is not None else ""
                print(f"#{r['id']:<5} {portfolio.ny_day(r['ts'])} {r['sub_code']:<3} {r['side']:<4} {r['symbol']:<8} "
                      f"{r['qty']:>10.4g} @ {r['price']:>9.2f}  kalan {r.get('qty_after', 0):.4g}{rz}  {r['note'] or ''}")
            print(f"({len(rows)}/{n})")
    except (portfolio.LedgerError, ValueError, IndexError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_cash(a) -> int:
    from . import portfolio
    try:
        if a.kind == "transfer":
            portfolio.transfer(a.amount, a.sub, a.to, ts=a.date, note=a.note)
        else:
            portfolio.add_cash(kind=a.kind, amount=a.amount, sub=a.sub, ts=a.date, currency=a.currency, note=a.note)
    except portfolio.LedgerError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print("✓ kaydedildi")
    return 0


def cmd_sub(a) -> int:
    from . import portfolio
    try:
        if a.action == "add":
            portfolio.add_sub(a.code, a.name or a.code)
        elif a.action == "rename":
            portfolio.update_sub(a.code, name=a.name)
        for s in portfolio.subportfolios(include_archived=True):
            print(f"{s['code']:<4} {s['name']}{' (arşiv)' if s['archived'] else ''}")
    except portfolio.LedgerError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_import(a) -> int:
    from . import portfolio
    from pathlib import Path
    from . import importers
    try:
        res = importers.import_text(Path(a.file).read_text(encoding="utf-8-sig"), a.sub, dry_run=a.dry_run, fmt=a.broker)
    except (OSError, portfolio.LedgerError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if not a.dry_run and (res["trades"] or res["cash"]):
        print("fiyatlar güncelleniyor…")
        portfolio.refresh_prices()
        from . import tickers
        added = tickers.sync_holdings()
        if added:
            print("izleme listesine eklendi:", " ".join(added))
    _print(res, True)
    return 0 if not res["errors"] else 2


def cmd_prices(a) -> int:
    from . import portfolio, prices
    out = prices.refresh(a.symbols) if a.symbols else portfolio.refresh_prices()
    print(" ".join(f"{k}:{v}" for k, v in sorted(out.items())))
    return 0


def cmd_setups(a) -> int:
    from . import setups

    def fmt_hit(k):
        return "—" if k["hit"] is None else f"%{k['hit']:.1f}"
    if a.action == "backfill":
        print(f"{setups.backfill(a.days, wide=a.wide)} geriye dönük setup")
        return 0
    if a.action == "run":
        setups.run_daily(use_llm=not a.no_llm)
    if a.action == "update":
        print(f"{setups.update_outcomes()} setup güncellendi")
        return 0
    if a.action == "record":
        for src in ("live", "backtest"):
            t = setups.track_record(src)
            x = t["all"]
            print(f"{src:<9} sonuçlanan {x['decided']:>4}  isabet {fmt_hit(x):>6}  beklenen {x['expectancy_r'] or 0:+.2f}R")
            for k in t["by_kind"]:
                warn = " ⚠ n<20" if k["decided"] < 20 else ""
                print(f"   {k['name']:<24} {k['decided']:>4}  {fmt_hit(k):>6}{warn}")
        return 0
    r = setups.report()
    if a.json:
        print(json.dumps({k: r[k] for k in ("day", "cards", "discovery", "wide", "open", "skipped", "summary")}, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"Setup'lar · T={r['day']} · {r['universe_n']} isim · rejim {r['regime']}\n{r['summary'] or ''}\n")
    for c in r["cards"]:
        print(f"{c['symbol']:<6} {c['name_tr']:<22} kalite {c['quality']:.0f}  giriş {c['entry_lo']:.2f}-{c['entry_hi']:.2f}  "
              f"stop {c['stop']:.2f}  H1 {c['t1']:.2f}  H2 {c['t2']:.2f}  R:R 1:{c['rr']:.1f}  → {c['valid_until']}")
    if r["discovery"]:
        w = r["wide"] or {}
        print(f"\nKeşif ({', '.join(w.get('lists') or [])}: {w.get('n', '?')} isim, eşiği geçen {w.get('found', '?')}):")
        for c in r["discovery"]:
            print(f"{c['symbol']:<6} {c['name_tr']:<22} kalite {c['quality']:.0f}  giriş {c['entry_lo']:.2f}-{c['entry_hi']:.2f}  "
                  f"stop {c['stop']:.2f}  H1 {c['t1']:.2f}  R:R 1:{c['rr']:.1f}  {(c.get('company') or '')[:28]} · {c.get('sector') or ''}")
    if r["open"]:
        print("\nAçık:")
        for o in r["open"]:
            print(f"  {o['symbol']:<6} {o['name_tr']:<22} {o['day']}  {o['status_tr']}")
    return 0


def cmd_karne(a) -> int:
    from . import scorecard
    if a.all:
        print(f"{scorecard.daily_job()} karne kaydedildi")
        return 0
    if not a.symbol:
        print("sembol gerekli (veya --all)")
        return 1
    sym = a.symbol.upper()
    if not a.show:
        scorecard.run(sym, use_llm=not a.no_llm)
    c = scorecard.latest(sym)
    if not c:
        print("karne yok")
        return 1
    if a.json:
        print(json.dumps(c, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"{sym}  {c['overall']:.0f}/100  {c['label']}   T={c['as_of']}  fiyat {c['price']:.2f}")
    for d in c["dims"]:
        sc = "—" if d["score"] is None else f"{d['score']:.0f}"
        print(f"  {d['name']:<22} {sc:>4}  (ağırlık {d['weight']})")
    fv = c["fair"]
    if fv.get("fair"):
        print(f"  Adil değer {fv['fair']:.2f}$ ({fv['upside']:+.1f}%), güven {fv['confidence']}")
    print(f"\n{c['summary']}\n  [{c['summary_model']}]")
    return 0


def cmd_risk(a) -> int:
    from . import risk
    r = risk.compute()
    if a.json:
        print(json.dumps(risk.brief(r), ensure_ascii=False, indent=2, default=str))
        return 0
    if not r or r.get("empty"):
        print("açık pozisyon yok")
        return 1
    print(r["summary"])
    b = risk.brief(r)
    print(f"Beta {b['beta']} · oynaklık %{b['vol_ex_ante_pct']} · VaR95 {b['var95_1d_usd']}$ (%{b['var95_1d_pct']}) · zirveden %{b['drawdown_from_peak_pct']}")
    for s in b["stress"]:
        print(f"  {s['name']:<55} {s['pnl_usd']:>9}$  %{s['pct']}")
    for w in b["warnings"]:
        print("! " + w)
    return 0


def cmd_tez(a) -> int:
    from . import thesis
    act = a.action
    if act in ("list", "ls"):
        rows = thesis.all_(include_closed=a.all)
        if a.json:
            print(json.dumps([thesis.brief(t) for t in rows], ensure_ascii=False, indent=2, default=str))
            return 0
        ov = thesis.overview()
        for t in rows:
            flag = " ⚠" if t["triggered"] or t["review_due"] else ""
            print(f"#{t['id']:<4} {t['symbol']:<6} {t['status_tr']:<13} sonraki {t['next_review'] or '—'}{flag}  {t['thesis'][:70]}")
        if ov["missing"]:
            print("Tezi olmayan pozisyonlar: " + ", ".join(ov["missing"]))
        return 0
    if act == "show":
        t = thesis.for_symbol(a.args[0], include_closed=True) if not a.args[0].isdigit() else thesis.get(int(a.args[0]))
        if not t:
            print("tez yok")
            return 1
        if a.json:
            print(json.dumps(thesis.brief(t), ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"#{t['id']} {t['symbol']} · {t['status_tr']} · {t['kind'] or ''} · sonraki gözden geçirme {t['next_review']}")
        print(t["thesis"])
        for r in t["rules"]:
            print(f"  [{r['status']}] {r['label']} — {r['last_value'] or ''}")
        for n in t["notes"][:10]:
            print(f"  {n['ts'][:10]} {n['text']}")
        return 0
    if act == "add":
        sym, text = a.args[0], " ".join(a.args[1:]) or a.text
        if not text:
            print("tez metni gerekli: borsa-radar tez add SYM \"neden...\" --rule 'stop 95'")
            return 2
        tid = thesis.add(sym, text, kind=a.kind, catalysts=a.catalysts, target=a.target, conviction=a.conviction,
                         review_days=a.review or 30, rules=a.rule or [], status="idea" if a.idea else "active")
        print(f"✓ tez #{tid} kaydedildi")
        return 0
    t = thesis.for_symbol(a.args[0]) if a.args and not a.args[0].isdigit() else (thesis.get(int(a.args[0])) if a.args else None)
    if act != "check" and not t:
        print("açık tez bulunamadı")
        return 1
    if act == "rule":
        k, p, n = thesis.parse_rule(" ".join(a.args[1:]))
        thesis.add_rule(t["id"], k, p, n)
        thesis.check(t["id"])
    elif act == "note":
        thesis.note(t["id"], " ".join(a.args[1:]))
    elif act == "review":
        thesis.reviewed(t["id"], " ".join(a.args[1:]) or None, keep=not a.weak)
    elif act == "close":
        print(json.dumps(thesis.close(t["id"], " ".join(a.args[1:]) or None), ensure_ascii=False))
    elif act == "delete":
        thesis.delete(t["id"])
    elif act == "check":
        fired = thesis.check(t["id"] if t else None)
        print(f"{len(fired)} yeni tetik")
        for f in fired:
            print(f"  {f['thesis']['symbol']}: {f['rule']['label']} — {f['value']}")
        return 0
    print("✓")
    return 0


def cmd_alarm(a) -> int:
    from . import alerts
    if a.action == "add":
        x = alerts.parse(" ".join(a.args))
        aid = alerts.add(x["symbol"], x["cond"], x["level"], note=a.note, repeat=a.repeat)
        print(f"✓ alarm #{aid}: {alerts.label(x)}")
        return 0
    if a.action in ("rm", "remove"):
        alerts.remove(int(a.args[0]))
        print("✓ silindi")
        return 0
    if a.action == "check":
        print(f"anlık {alerts.check_live()} · kapanış {alerts.check_close()} tetik")
        return 0
    rows = alerts.list_(a.args[0] if a.args else None, include_inactive=a.all)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    for r in rows:
        print(f"#{r['id']:<4} {r['label']}{' (tekrarlı)' if r['repeat'] else ''}"
              + (f" — tetiklendi {r['fired_at'][:16]} @ {r['fired_value']:.2f}" if r["fired_at"] else ""))
    return 0


def cmd_context(a) -> int:
    from . import context
    if a.symbol:
        data = context.stock(a.symbol)
    else:
        data = context.snapshot()
    if a.json:
        print(context.dumps(data))
        return 0
    if a.symbol:
        d = data
        print(f"{d['symbol']} · {d['name']} · T={d['as_of']} · fiyat {d['price']}  {d['url']}")
        if d["position"]:
            p = d["position"]
            print(f"Pozisyon: {p['qty']} adet, ort. {p['avg']}, değer {p['value']}$, K/Z %{p['unreal_pct']}")
        if d["karne"]:
            k = d["karne"]
            print(f"Karne ({k['day']}): {k['overall']}/100 {k['label']} — {k['summary']}")
        for s in d["sections"].values():
            print(f"[{s['title']}] {s['result']}")
        for s in d["setups"]:
            print(f"Setup: {s['name_tr']} ({s['day']}) {s['status_tr']} giriş {s['entry_lo']}-{s['entry_hi']} stop {s['stop']} H1 {s['t1']}")
        for e in d["events_30d"][:8]:
            print(f"  {e['at']} önem {e['importance']} {e['title']}")
        return 0
    print(context.text(data))
    if data["events_24h"]:
        print("\nÖNEMLİ GELİŞMELER (24 saat):")
        for e in data["events_24h"]:
            print(f"- {e['at']} [{e['symbol'] or 'MAKRO'}] önem {e['importance']}: {e['title']}")
    if data["calendar_72h"]:
        print("\nTAKVİM (72 saat):")
        for c in data["calendar_72h"]:
            print(f"- {c['at']} {c['name']} (önem {c['importance']})")
    return 0


def cmd_piyasa(a) -> int:
    from . import breadth, regime
    if a.refresh:
        print(f"genişlik: {breadth.refresh()} gün")
    if a.refresh or a.snapshot:
        regime.snapshot(use_llm=not a.no_llm)
    from . import market_report
    rep = market_report.build()
    if a.json:
        print(json.dumps({k: rep[k] for k in ("day", "regime", "rotation", "breadth", "sicil")}, ensure_ascii=False, indent=2, default=str))
        return 0
    r = rep["regime"]
    if not r:
        print("rejim hesaplanamadı (fiyat verisi yok: borsa-radar piyasa --refresh)")
        return 1
    print(f"T = {rep['day']}  Rejim: {r['label']}  {r['overall']:.0f}/100   İklim {r['climate']:.1f}/10   Hava {r['weather']:+d}")
    for k, c in r["comps"].items():
        print(f"  {c['name']:<22} {c['score']:>5.0f}  {'; '.join(c['why'])}")
    print(f"\n{rep['summary']}  [{rep['summary_model']}]")
    ro = rep["rotation"]
    print(f"\nRotasyon: {ro['result']}")
    for q, syms in ro["by_quad"].items():
        print(f"  {q:<11} {' '.join(syms)}")
    b = rep["breadth"]
    if b:
        print(f"\nGenişlik ({b['day']}): 200g üstü %{b['above200']:.1f} · 50g üstü %{b['above50']:.1f} · A/D {b['adv'] - b['dec']:+d} · NH−NL {b['nh'] - b['nl']:+d}")
    return 0


def cmd_test_telegram(a) -> int:
    from . import notifier
    ok = notifier.send_now("✅ <b>Borsa Radar</b> bağlantı testi — bildirimler bu sohbete gelecek.")
    print("gönderildi" if ok else "başarısız (status'e bakın)")
    return 0 if ok else 1


def cmd_serve(a) -> int:
    import uvicorn
    uvicorn.run("borsa_radar.web.app:app", host=a.host, port=a.port, log_level="info", proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="borsa-radar", description="Hisse/makro haber radarı")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("ticker", help="izleme listesi: add / remove / list / set")
    t.add_argument("action", choices=["add", "remove", "rm", "list", "ls", "set"])
    t.add_argument("symbols", nargs="*")
    t.add_argument("--notes")
    t.add_argument("--threshold", type=int, help="bildirim eşiği 1-5 (hisseye özel)")
    t.add_argument("--clear-threshold", action="store_true")
    t.add_argument("--purge", action="store_true", help="remove: geçmişiyle tamamen sil")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_ticker)

    e = sub.add_parser("events", help="son gelişmeler")
    e.add_argument("--symbol", "-s")
    e.add_argument("--type", choices=["news", "rating", "filing", "macro", "data", "reg", "contract", "flow"])
    e.add_argument("--days", type=float, default=3)
    e.add_argument("--min-importance", type=int)
    e.add_argument("--limit", type=int, default=30)
    e.add_argument("--json", action="store_true")
    e.set_defaults(fn=cmd_events)

    ev = sub.add_parser("event", help="tek olay detayı (JSON)")
    ev.add_argument("id", type=int)
    ev.set_defaults(fn=cmd_event)

    c = sub.add_parser("calendar", help="ekonomik takvim + bilançolar")
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_calendar)

    sub.add_parser("status", help="sistem durumu").set_defaults(fn=cmd_status)
    sub.add_parser("run", help="tek seferlik tam döngü").set_defaults(fn=cmd_run)

    d = sub.add_parser("digest", help="özet üret / gönder")
    d.add_argument("kind", choices=["morning", "evening"])
    d.add_argument("--send", action="store_true")
    d.set_defaults(fn=cmd_digest)

    an = sub.add_parser("analyze", help="bir olayı LLM ile analiz et")
    an.add_argument("id", type=int)
    an.set_defaults(fn=cmd_analyze)

    rs = sub.add_parser("research", help="olay için web'de derin araştırma (Hermes)")
    rs.add_argument("id", type=int)
    rs.add_argument("--now", action="store_true", help="kuyruğu beklemeden çalıştır ve sonucu yaz")
    rs.set_defaults(fn=cmd_research)

    fu = sub.add_parser("fundamentals", help="SEC XBRL çeyrek finansalları")
    fu.add_argument("symbol")
    fu.add_argument("-n", type=int, default=8)
    fu.add_argument("--json", action="store_true")
    fu.set_defaults(fn=cmd_fundamentals)

    sub.add_parser("odds", help="Kalshi tahmin piyasası olasılıkları").set_defaults(fn=cmd_odds)

    po = sub.add_parser("positioning", help="açığa satış + Reddit ilgisi")
    po.add_argument("symbol")
    po.set_defaults(fn=cmd_positioning)

    pf = sub.add_parser("portfolio", help="portföy özeti (değer, getiri, alt portföyler, pozisyonlar)")
    pf.add_argument("--top", type=int, default=25)
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(fn=cmd_portfolio)

    tr = sub.add_parser("trade", help="işlem: add SIDE SYMBOL QTY PRICE / list / rm ID / move SYMBOL SUB")
    tr.add_argument("action", choices=["add", "list", "ls", "rm", "remove", "move"])
    tr.add_argument("args", nargs="*")
    tr.add_argument("--sub", default="P1")
    tr.add_argument("--date", help="ISO tarih/saat (varsayılan: şimdi)")
    tr.add_argument("--fees", type=float, default=0)
    tr.add_argument("--currency", default="USD")
    tr.add_argument("--asset", choices=["stock", "etf", "option", "crypto"])
    tr.add_argument("--note")
    tr.add_argument("--symbol")
    tr.add_argument("--limit", type=int, default=30)
    tr.add_argument("--json", action="store_true")
    tr.set_defaults(fn=cmd_trade)

    ca = sub.add_parser("cash", help="nakit hareketi: deposit / withdrawal / dividend / fee / interest / transfer")
    ca.add_argument("kind", choices=["deposit", "withdrawal", "dividend", "interest", "fee", "adjustment", "transfer"])
    ca.add_argument("amount", type=float)
    ca.add_argument("--sub", default="P1")
    ca.add_argument("--to", help="transfer hedefi (alt portföy kodu)")
    ca.add_argument("--date")
    ca.add_argument("--currency", default="USD")
    ca.add_argument("--note")
    ca.set_defaults(fn=cmd_cash)

    sp = sub.add_parser("sub", help="alt portföyler: list / add KOD AD / rename KOD AD")
    sp.add_argument("action", choices=["list", "add", "rename"], nargs="?", default="list")
    sp.add_argument("code", nargs="?")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(fn=cmd_sub)

    im = sub.add_parser("import", help="işlem geçmişi içe aktar: revolut dökümü ya da genel CSV (date,type,symbol,quantity,price,fees,amount,currency)")
    im.add_argument("broker", choices=["auto", "csv", "revolut"], help="auto: başlıktan anla")
    im.add_argument("file")
    im.add_argument("--sub", default="P1")
    im.add_argument("--dry-run", action="store_true")
    im.set_defaults(fn=cmd_import)

    pr = sub.add_parser("prices", help="günlük fiyat deposunu güncelle")
    pr.add_argument("symbols", nargs="*")
    pr.set_defaults(fn=cmd_prices)

    se = sub.add_parser("setups", help="setup tarayıcı: göster / run / update / record / backfill")
    se.add_argument("action", nargs="?", default="show", choices=["show", "run", "update", "record", "backfill"])
    se.add_argument("--no-llm", action="store_true")
    se.add_argument("--days", type=int, default=500)
    se.add_argument("--wide", action="store_true", help="backfill: keşif evrenini de (endeks listeleri) geriye dönük tara")
    se.add_argument("--json", action="store_true")
    se.set_defaults(fn=cmd_setups)

    ka = sub.add_parser("karne", help="hisse karnesi: hesapla ve göster (KARNE SYMBOL, --show, --no-llm, --all)")
    ka.add_argument("symbol", nargs="?")
    ka.add_argument("--show", action="store_true", help="yeniden hesaplamadan son karneyi göster")
    ka.add_argument("--no-llm", action="store_true", help="özet cümlesi şablondan")
    ka.add_argument("--all", action="store_true", help="portföy + izleme listesi (günlük iş)")
    ka.add_argument("--json", action="store_true")
    ka.set_defaults(fn=cmd_karne)

    pi = sub.add_parser("piyasa", help="piyasa rejimi, rotasyon, genişlik (--refresh: S&P 500 verisini güncelle)")
    pi.add_argument("--refresh", action="store_true", help="bileşen fiyatları + genişlik + rejim (yavaş, ~2 dk)")
    pi.add_argument("--snapshot", action="store_true", help="bugünkü rejimi kaydet (özet cümlesiyle)")
    pi.add_argument("--no-llm", action="store_true")
    pi.add_argument("--json", action="store_true")
    pi.set_defaults(fn=cmd_piyasa)

    rk = sub.add_parser("risk", help="portföy riski: beta, korelasyon kümeleri, VaR, stres testleri, uyarılar")
    rk.add_argument("--json", action="store_true")
    rk.set_defaults(fn=cmd_risk)

    tz = sub.add_parser("tez", help="tez defteri: list / show SYM / add SYM \"metin\" --rule ... / rule SYM 'stop 95' / note / review / close / check")
    tz.add_argument("action", choices=["list", "ls", "show", "add", "rule", "note", "review", "close", "delete", "check"])
    tz.add_argument("args", nargs="*")
    tz.add_argument("--text")
    tz.add_argument("--rule", action="append", help="tekrarlanabilir: 'stop 95', 'hedef 250', 'zarar 30', 'karne 40', 'gelir 20', 'sulandırma 5', 'rejim 40', 'tarih 2027-06-30 ...'")
    tz.add_argument("--kind")
    tz.add_argument("--catalysts")
    tz.add_argument("--target", type=float)
    tz.add_argument("--conviction", type=int)
    tz.add_argument("--review", type=int, help="kaç günde bir gözden geçir (varsayılan 30)")
    tz.add_argument("--idea", action="store_true", help="pozisyon yok, fikir olarak kaydet")
    tz.add_argument("--weak", action="store_true", help="review: tez zayıfladı")
    tz.add_argument("--all", action="store_true", help="list: kapananlar dahil")
    tz.add_argument("--json", action="store_true")
    tz.set_defaults(fn=cmd_tez)

    al = sub.add_parser("alarm", help="fiyat alarmları: add 'NVDA kapanış 150 altı' / list / rm ID / check")
    al.add_argument("action", choices=["add", "list", "ls", "rm", "remove", "check"])
    al.add_argument("args", nargs="*")
    al.add_argument("--note")
    al.add_argument("--repeat", action="store_true", help="her işlem günü en fazla bir kez tekrar et")
    al.add_argument("--all", action="store_true")
    al.add_argument("--json", action="store_true")
    al.set_defaults(fn=cmd_alarm)

    cx = sub.add_parser("context", help="Hermes için tek bakışta her şey: portföy, rejim, setup'lar, gelişmeler (--symbol: tek hisse)")
    cx.add_argument("--symbol", "-s")
    cx.add_argument("--json", action="store_true")
    cx.set_defaults(fn=cmd_context)

    sub.add_parser("test-telegram").set_defaults(fn=cmd_test_telegram)

    # plug-in commands: cli_ext_<name>.py exposing register(sub); loaded if present
    import importlib
    for m in ("behavior", "precheck", "tax", "earnings", "peers", "notify", "karar", "lab", "fikir", "tara"):
        try:
            importlib.import_module(f".cli_ext_{m}", __package__).register(sub)
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.cli_ext_{m}":
                raise

    s = sub.add_parser("serve", help="web arayüzü + zamanlayıcı")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=config.HTTP_PORT)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .util import install_log_redaction
    install_log_redaction()
    db.init()
    args = build_parser().parse_args(argv)
    return args.fn(args)
