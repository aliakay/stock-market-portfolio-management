"""`borsa-radar karar [SYM | run [--no-llm] | sicil] [--json]` — sonuç katmanı: portföy panosu ve hisse başına karar kartı."""

from __future__ import annotations

import json

from .util import tr_n


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _signals(title: str, rows: list[dict]) -> None:
    if not rows:
        return
    print(f"  {title}:")
    for s in sorted(rows, key=lambda x: -(x.get("w") or 0)):
        print(f"    {'●' * (s.get('w') or 1):<3} {s['text']}" + (f"  [{s['src']}]" if s.get("src") else ""))


def _print_card(c: dict) -> None:
    print(f"{c['symbol']} · {c['stance_tr']}" + (f" · {c['pattern_tr']}" if c.get("pattern_tr") else "")
          + f"  (T {c.get('as_of') or '—'}, kurallar {c.get('rules_ver') or '—'})")
    print(f"  {c.get('headline') or '—'}")
    if c.get("changed"):
        ch = c["changed"]
        print(f"  Önceki durum: {ch.get('from_tr')} ({ch.get('day') or '—'})")
    if c.get("comment"):
        print(f"  Yorum ({c.get('comment_model') or 'model'}): {c['comment']}")
    p = c.get("position")
    if p:
        print(f"  Pozisyon: ağırlık %{tr_n(p.get('weight'), 1)} · K/Z %{tr_n(p.get('unreal_pct'), 1, True)} · "
              f"risk payı %{tr_n(p.get('risk_share'), 0)} · beta {tr_n(p.get('beta'))}")
    _signals("Lehine", c.get("for") or [])
    _signals("Aleyhine", c.get("against") or [])
    ch = c.get("changers") or {}
    for x in ch.get("up") or []:
        print(f"  ▲ {x['text']}")
    for x in ch.get("down") or []:
        print(f"  ▼ {x['text']}")
    for x in c.get("next") or []:
        print(f"  Tarih {x['day']}: {x['text']}")
    for x in c.get("todo") or []:
        print(f"  Yapılacak: {x['text']}" + (f" → {x['href']}" if x.get("href") else ""))
    for s in c.get("notes") or []:
        print(f"  · {s['text']}")


def _print_board(b: dict) -> None:
    print(b.get("headline") or "—")
    m = b.get("market") or {}
    if m:
        print(f"Piyasa: {m.get('label') or '—'}" + (f" {tr_n(m['score'], 0)}/100" if m.get("score") is not None else "")
              + (f" — {m['text']}" if m.get("text") else ""))
    print("Durumlar: " + (" · ".join(f"{c['stance_tr']} {c['n']}" + (f" (%{tr_n(c['weight_pct'], 0)})" if c.get("weight_pct") is not None else "")
                                    for c in b.get("stance_counts") or [] if c.get("n")) or "—"))
    if b.get("comment"):
        print(f"Yorum ({b.get('comment_model') or 'model'}): {b['comment']}")
    if b.get("agenda"):
        print("\nGündem:")
        for i, a in enumerate(b["agenda"], 1):
            print(f"  {i}. {a['text']}")
    if b.get("changes"):
        print("\nDeğişenler:")
        for c in b["changes"]:
            print(f"  {c['symbol']}: {c['from_tr']} → {c['to_tr']} ({c.get('day') or '—'})")
    print("\nHisseler:")
    for c in b.get("cards") or []:
        print(f"  {c['symbol']:<6} {c['stance_tr']:<14} {c.get('headline') or ''}")
    print(f"\nT = {b.get('as_of') or '—'} · ayrıntı: borsa-radar karar SEMBOL · etiketler durum tarifidir, emir değildir.")


def cmd_karar(a) -> int:
    from . import karar
    arg = (a.what or "").strip()
    if arg.lower() == "run":
        b = karar.run(use_llm=not a.no_llm)
        if a.json:
            _dump(b)
        else:
            _print_board(b)
        return 0
    if arg.lower() == "sicil":
        tr = karar.track_record(20)
        if a.json:
            _dump(tr)
            return 0
        print(f"Sicil — durumlara göre sonraki {tr['horizon']} işlem günü (S&P 500'e göre fark), {tr['cards']} kayıt")
        for r in tr["rows"]:
            print(f"  {r['label']:<14} n={r['n']:<4} ort. fark {('%' + tr_n(r['avg'], 1, True)) if r['avg'] is not None else '—':<9}"
                  f" isabet {('%' + tr_n(r['hit'], 0)) if r['hit'] is not None else '—'}")
        if (tr.get("n") or 0) < 20:
            print(f"Örnek küçük (n = {tr.get('n') or 0} < 20): bu oranlar henüz güvenilir değil.")
        return 0
    if arg:
        c = karar.latest(arg.upper())
        if not c:
            print(f"{arg.upper()}: karar kartı yok (borsa-radar karar run)")
            return 1
        if a.json:
            _dump(c)
        else:
            _print_card(c)
        return 0
    b = karar.board()
    if not b:
        print("Henüz karar hesaplanmadı: borsa-radar karar run")
        return 1
    if a.json:
        _dump(b)
    else:
        _print_board(b)
    return 0


def register(sub) -> None:
    p = sub.add_parser("karar", help="sonuç katmanı: portföy panosu; karar SEMBOL (tek kart) · karar run [--no-llm] · karar sicil")
    p.add_argument("what", nargs="?", help="sembol, 'run' ya da 'sicil' (boşsa pano)")
    p.add_argument("--no-llm", action="store_true", help="run: model yorumu olmadan hesapla")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_karar)
