"""CLI: borsa-radar bilanco SYM | bilancolar | haftalik."""

from __future__ import annotations

import json

from .util import tr_n


def _p(v, d: int = 1) -> str:
    return "—" if v is None else "%" + tr_n(v, d, sign=True)


def cmd_bilanco(a) -> int:
    from . import earnings
    b = earnings.build(a.symbol)
    if a.json:
        print(json.dumps(earnings.brief(a.symbol, b), ensure_ascii=False, indent=2, default=str))
        return 0
    print(b["summary"])
    if b["post"]:
        p = b["post"]
        print(f"\nNe değişti ({p['date']}, {p['timing_tr']}):")
        for line in p["what_changed"]:
            print("  • " + line)
    if b["history"]:
        print("\nGeçmiş raporlar:")
        for r in b["history"]:
            rx = r.get("reaction") or {}
            print(f"  {r['date']} {r['timing_tr']:<15} EPS {tr_n(r['eps_act'])} / {tr_n(r['eps_est'])} ({_p(r['surprise'])})"
                  f"  boşluk {_p(rx.get('gap_pct'))}  ilk seans {_p(rx.get('move_pct'))}  ertesi {_p(rx.get('next_pct'))}")
    if b["missing"]:
        print("\ndoğrulanamadı: " + ", ".join(b["missing"]))
    print("\nKaynak: " + " | ".join(b["sources"]))
    return 0


def cmd_bilancolar(a) -> int:
    from . import earnings
    u = earnings.upcoming(a.days, quick=not a.live)
    if a.json:
        print(json.dumps({"days": a.days, "upcoming": [earnings.brief(b["symbol"], b) | {"held": b["held"]} for b in u["soon"]],
                          "recent": [earnings.brief(b["symbol"], b) | {"held": b["held"]} for b in u["recent"]],
                          "unknown": u["unknown"]}, ensure_ascii=False, indent=2, default=str))
        return 0
    if not u["soon"]:
        print(f"Önümüzdeki {a.days} günde bilanço yok.")
    for b in u["soon"]:
        n, im, st, ex = b["next"], b["implied"], b["stats"], b["exposure"]
        line = (f"{'★' if b['held'] else ' '} {n['date']} {b['symbol']:<6} {n['timing_tr']:<15}"
                f" EPS bekl. {tr_n(b['est']['eps'] if b['est']['eps'] is not None else n.get('eps'))}"
                f"  opsiyon {'±%' + tr_n(im['pct'], 1) if im else '—'}"
                f"  geçmiş ±%{tr_n(st['avg_abs'], 1)}")
        if ex["held"] and ex["risk_implied"] is not None:
            line += f"  risk ±{tr_n(ex['risk_implied'], 0)}$"
        print(line)
    for b in u["recent"]:
        p = b["post"]
        rx = p.get("reaction") or {}
        print(f"  son rapor {p['date']} {b['symbol']:<6} EPS {tr_n(p['eps_act'])} / {tr_n(p['eps_est'])} ilk seans {_p(rx.get('move_pct'))}")
    return 0


def cmd_haftalik(a) -> int:
    from . import weekly
    if a.send:
        ok = weekly.send(force=a.force)
        print("gönderildi" if ok else "gönderilmedi (bugün zaten gönderilmiş ya da Telegram kapalı)")
        return 0 if ok else 1
    text = weekly.build()
    print(json.dumps({"text": text, "chars": len(text)}, ensure_ascii=False, indent=2) if a.json else text)
    return 0


def register(sub) -> None:
    b = sub.add_parser("bilanco", help="tek hisse bilanço önizlemesi / sonrası (tarih, beklenti, opsiyon hareketi, geçmiş tepki)")
    b.add_argument("symbol", type=str.upper)
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_bilanco)

    bl = sub.add_parser("bilancolar", help="portföy + izleme listesinde yaklaşan bilançolar")
    bl.add_argument("--days", type=int, default=14)
    bl.add_argument("--live", action="store_true", help="FMP ve opsiyon zincirlerini önbellek yerine yeniden çek")
    bl.add_argument("--json", action="store_true")
    bl.set_defaults(fn=cmd_bilancolar)

    h = sub.add_parser("haftalik", help="Pazar 'Haftaya bakış' özeti (varsayılan: yazdır)")
    h.add_argument("--send", action="store_true", help="Telegram'a gönder ve kaydet (bugün gönderildiyse atla)")
    h.add_argument("--force", action="store_true", help="--send ile: bugün gönderilmiş olsa da gönder")
    h.add_argument("--json", action="store_true")
    h.set_defaults(fn=cmd_haftalik)
