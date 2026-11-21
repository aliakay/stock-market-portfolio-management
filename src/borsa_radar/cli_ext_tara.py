"""`borsa-radar tara [run | <hazır tarama>] [-n N]` — hisse tarayıcı: gece verisini yenile ya da hazır bir taramayı listele."""

from __future__ import annotations

import sys
import time

from .util import tr_n


def cmd_tara(args) -> int:
    from . import screener
    what = args.what or ""
    if what == "run":
        t0 = time.time()
        r = screener.run(refresh_prices=not args.no_prices, fill_yahoo=not args.no_yahoo, progress=lambda m: print(" ", m, file=sys.stderr, flush=True))
        print(f"tarama verisi hazır: {r['n']} hisse (evren {r['universe']}, finansalı olan {r['with_fund']}, bunun {r['with_yahoo']}'i Yahoo'dan, "
              f"bilanço tarihi {r['with_earn']}) · "
              f"fiyatlar {r['price_day']} · {time.time() - t0:.0f} sn")
        return 0
    run, rows = screener.latest()
    if not run:
        print("henüz tarama verisi yok: borsa-radar tara run")
        return 1
    if what and what not in screener.PRESETS:
        print("hazır taramalar: " + ", ".join(f"{k} ({p['name']})" for k, p in screener.PRESETS.items()))
        return 1
    flt, key, desc, pre = screener.resolve({"preset": what} if what else {})
    hits = screener.sort(screener.apply(rows, flt), key, desc)
    print(f"{pre['name'] if pre else 'Tüm hisseler'}: {len(hits)} hisse · veri {run['price_day']}")
    for r in hits[:args.n]:
        print(f"  {r['symbol']:<6} {(r.get('name') or '')[:28]:<28} {tr_n(r.get('mcap'), 1):>7} Mr$ · gelir %{tr_n(r.get('rev_yoy'), 0, True)} · "
              f"6 ay %{tr_n(r.get('r6m'), 0, True)} · zirveye %{tr_n(r.get('off_hi'), 0, True)}" + (f" · bilanço {r['earn']}" if r.get("earn") else ""))
    return 0


def register(sub) -> None:
    p = sub.add_parser("tara", help="hisse tarayıcı: tara run (veriyi yenile) · tara <hazır tarama> · boşsa en büyük şirketler")
    p.add_argument("what", nargs="?", help="run | kara_yakin | ilk_kar | canslim | momentum | asiri_satis | ucuz_buyuyen | deger_kalite | sert_dusen | hacim | dengeleyen | bilanco")
    p.add_argument("-n", type=int, default=20, help="kaç satır")
    p.add_argument("--no-prices", action="store_true", help="fiyatları indirmeden mevcut verilerle hesapla")
    p.add_argument("--no-yahoo", action="store_true", help="SEC'te eksik çeyrekler için Yahoo gelir tablolarını kullanma")
    p.set_defaults(fn=cmd_tara)
