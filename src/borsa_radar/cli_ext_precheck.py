"""`borsa-radar kontrol buy NVDA 10 [--price P] [--stop S] [--amount A] [--json]` — işlem öncesi kontrol."""

from __future__ import annotations

import json

ICON = {"ok": "✓", "na": "?", "warn": "!", "stop": "✗"}


def cmd_kontrol(args) -> int:
    from . import precheck
    try:
        r = precheck.check(args.side, args.symbol, qty=args.qty, amount=args.amount, price=args.price, stop=args.stop)
    except ValueError as exc:
        print(f"hata: {exc}")
        return 2
    if args.json:
        print(json.dumps(precheck.brief(r), ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"{r['verdict']} · {r['summary']}")
    print(f"Fiyat {precheck._px(r['price'])} ({r['price_src']}) · portföy {precheck._usd(r['portfolio']['value'])} ({r['portfolio']['src']})")
    for c in r["checks"]:
        print(f"  [{ICON[c['status']]}] {c['title']} ({c['status_tr']}): {c['reason']}")
        if c.get("source"):
            print(f"      kaynak: {c['source']}")
        if c.get("link"):
            print(f"      → {c['link']}")
    for n in r["notes"]:
        print(f"  · {n}")
    ctx = r.get("context") or {}
    if ctx.get("karne"):
        k = ctx["karne"]
        print(f"  Karne: {precheck.tr_n(k['overall'], 1)} {k.get('label') or ''} ({precheck._d(k.get('as_of'))})")
    if ctx.get("setup"):
        s = ctx["setup"]
        print(f"  Açık setup ({s['kind']}, {precheck._d(s['day'])}, {s['status']}): giriş {precheck._px(s['entry_lo'])}–{precheck._px(s['entry_hi'])}, "
              f"stop {precheck._px(s['stop'])}, hedef {precheck._px(s['t1'])} / {precheck._px(s.get('t2'))}")
    print("Öneri ve kayıt aracıdır; emir vermez, işlemi engellemez.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("kontrol", help="işlem öncesi kontrol: boyut, ağırlık, yoğunlaşma, rejim, devre kesici, tez, bilanço")
    p.add_argument("side", choices=["buy", "sell", "al", "sat"])
    p.add_argument("symbol")
    p.add_argument("qty", nargs="?", type=float, help="adet (boşsa --amount ya da önerilen adet / satışta tamamı)")
    p.add_argument("--amount", type=float, help="adet yerine dolar tutarı")
    p.add_argument("--price", type=float, help="giriş/çıkış fiyatı (varsayılan son kapanış)")
    p.add_argument("--stop", type=float, help="stop fiyatı (varsayılan açık setup stopu ya da fiyat − 2×ATR)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_kontrol)
