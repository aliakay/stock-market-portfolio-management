"""`borsa-radar fikir [run | backtest | sec]` — kural tabanlı hisse fikirleri (SEC finansalları + Yahoo)."""

from __future__ import annotations

import sys

from .util import tr_n


def cmd_fikir(args) -> int:
    from . import frames, ideas
    what = args.what or "show"
    if what == "sec":
        r = frames.refresh(progress=lambda k, n: print(f"  {k}/{n}", file=sys.stderr, flush=True))
        print(f"SEC frames: {r['jobs']} çağrı, {r['fetched']} yeni")
        return 0
    if what == "backtest":
        r = ideas.backtest()
        for code, (title, _) in ideas.SCREENS.items():
            for v in ("all", "trend"):
                s = r["screens"][code][v]["126"]
                if s.get("n"):
                    print(f"{title:22} {v:5}: {s['n']:5} seçim · 6 ay ort. %{tr_n(s['avg'], 1, True)} · ortalamaya göre %{tr_n(s['ex_uni'], 1, True)} · "
                          f"S&P'yi yenen %{tr_n(s['beat_spy'], 0)}")
        return 0
    if what == "run":
        r = ideas.run(progress=lambda m: print(f"  {m}", file=sys.stderr, flush=True), refresh=not args.no_refresh)
    else:
        r = ideas.latest()
        if not r:
            print("Henüz tarama yok: borsa-radar fikir run")
            return 0
    print(f"{r['day']} · {r['universe']} şirket · aday {r['candidates']}")
    for code, (title, _) in ideas.SCREENS.items():
        rs = [x for x in r["lists"].get(code, []) if x.get("shown", 1)][:args.n]
        print(f"\n{title} ({r['counts'].get(code, 0)}):")
        for x in rs:
            y = x.get("y") or {}
            print(f"  {x['symbol']:6} {str(x['name'])[:28]:28} gelir %{tr_n((x['rev_yoy'] or 0) * 100, 0, True)}"
                  + (f" · ~{tr_n(x['q_be'], 0)} çeyrekte başa baş" if x.get("q_be") else "")
                  + (f" · HBK gelecek yıl {tr_n(y.get('eps_next_y'), 2)}" if y.get("eps_next_y") is not None else "")
                  + (" · 200g üstü" if x.get("trend_up") else " · 200g altı"))
    return 0


def register(sub) -> None:
    p = sub.add_parser("fikir", help="kural tabanlı hisse fikirleri: fikir run · fikir backtest · fikir sec · boşsa son liste")
    p.add_argument("what", nargs="?", help="run | backtest | sec | boş")
    p.add_argument("--no-refresh", action="store_true", help="run: SEC verisini yeniden çekme")
    p.add_argument("-n", type=int, default=8)
    p.set_defaults(fn=cmd_fikir)
