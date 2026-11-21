"""`borsa-radar davranis`: trade behavior analytics (process coaching, not buy/sell advice)."""

import json


def cmd_davranis(a) -> int:
    from . import behavior
    from .util import tr_n
    r = behavior.compute()
    if a.json:
        print(json.dumps(behavior.brief(r), ensure_ascii=False, indent=2, default=str))
        return 0
    if not r or r.get("empty"):
        print("kapanan işlem yok")
        return 1
    print(r["summary"])
    print()
    for b in r["holding"]:
        print(f"  {b['label']:<10} n={b['n']:<3} isabet %{tr_n(b['hit'], 0):<4} beklenti {tr_n(b['exp'], 0, sign=True):>6}$  toplam {tr_n(b['sum'], 0, sign=True):>7}$")
    print()
    for f in r["findings"]:
        mark = {"warn": "!", "info": "·", "ok": "✓"}[f["tone"]]
        print(f"{mark} {f['title']} (n={f['n']}{', küçük örneklem' if f['note'] else ''})")
        print(f"  {f['text']}")
        print(f"  ? {f['question']}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("davranis", help="işlem davranışı analizi: isabet, beklenti, zarardan sonra işlem, ortalama düşürme, satış sonrası fiyat")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_davranis)
