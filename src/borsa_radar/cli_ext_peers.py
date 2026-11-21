"""CLI: `borsa-radar karsilastir NVDA AMD AVGO [--json]` and `borsa-radar peers SYM [--json]`."""

from __future__ import annotations

import sys


def _fmt(v, kind: str) -> str:
    from .hub import big, num
    from .peers import isnum
    if not isnum(v):
        return "—"
    if kind == "big":
        return big(v)
    if kind == "pct":
        return "%" + num(v, 1)
    return num(v, 1) + "x"


def _pos_text(m: dict) -> str:
    from .hub import num
    if not m["target_usable"]:
        return "—"
    parts = []
    if m["kind"] == "qual" and m["diff_pts"] is not None:
        parts.append(f"{m['diff_pts']:+.1f} puan".replace(".", ","))
    elif m["kind"] == "lev" and m["diff_pts"] is not None:
        parts.append(f"{m['diff_pts']:+.1f}x fark".replace(".", ","))
    elif m["premium_pct"] is not None:
        word = "prim" if m["premium_pct"] >= 0 else "iskonto"
        parts.append(f"%{abs(m['premium_pct']):.0f} {word}".replace(".", ","))
    if m["percentile"] is not None:
        parts.append(f"yüzdelik {num(m['percentile'], 0)}")
    return " · ".join(parts) or "—"


def cmd_karsilastir(a) -> int:
    from . import peers
    try:
        res = peers.compare(a.symbols)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(peers.dumps(res))
        return 0
    syms = [res["target"], *res["peers"]]
    w = max(10, *(len(s) + 2 for s in syms))
    print(f"Akran karşılaştırması: {res['target']} vs {', '.join(res['peers'])}   {res['url']}")
    print(f"{'Ölçü':<34}" + "".join(f"{s:>{w}}" for s in syms) + f"{'Akran medyanı':>15}  {res['target']} konumu")
    for key, label, kind, fmt in peers.METRICS:
        m = res["metrics"][key]
        vals = ""
        for s in syms:
            c = res["table"][s][key]
            txt = _fmt(c["value"], fmt)
            if c["value"] is not None and not peers.meaningful(key, c["value"]):
                txt += "!"
            elif c["source"] and c["source"].startswith("Yahoo"):
                txt += "*"
            vals += f"{txt:>{w}}"
        pos = _pos_text(m) if kind != "size" else ""
        print(f"{label[:33]:<34}{vals}{_fmt(m['median'], fmt):>15}  {pos}")
    print("  * Yahoo yedeği   ! anlamsız (negatif kazanç/FAVÖK/SNA), medyana katılmaz   — doğrulanamadı")
    lab = res["label"]
    print()
    print(f"Sonuç: {res['target']} {lab['label']} — {lab['why']}")
    print(res["rule"])
    if lab.get("quality_note"):
        print(lab["quality_note"])
    g = res["growth_adjusted_pe"]
    gtxt = ", ".join(f"{s} {_fmt(v, 'x')}" for s, v in g["by_symbol"].items())
    print(f"Büyümeye göre düzeltilmiş F/K: {gtxt}; akran medyanı {_fmt(g['peer_median'], 'x')}"
          + (f", {res['target']} {peers.signed_pct(g['target_premium_pct'], 0)}" if g["target_premium_pct"] is not None else ""))
    print("  " + g["rule"])
    print(res["caveat"])
    if res.get("source_note"):
        print(res["source_note"])
    miss = {s: m for s, m in res["missing"].items() if m}
    if miss:
        print("Doğrulanamadı: " + "; ".join(f"{s}: {', '.join(m)}" for s, m in miss.items()))
    print("Kaynaklar:")
    for s in syms:
        info = res["symbols"][s]
        print(f"  {s} ({info.get('name') or '—'} · {info.get('industry') or '—'}): " + " | ".join(info["sources"]))
    return 0


def cmd_peers(a) -> int:
    from . import peers
    res = peers.suggest(a.symbol, limit=a.limit)
    if a.json:
        print(peers.dumps(res))
        return 0
    print(f"{res['symbol']} ({res.get('name') or '—'}) · {res.get('industry') or '—'} / {res.get('sector') or '—'}")
    print(f"Kaynak: {res['source']}")
    for p in res["peers"]:
        print(f"  {p['symbol']:<7} {(p.get('name') or '')[:38]:<38} {p.get('why') or ''}")
    if not res["peers"]:
        print("  akran bulunamadı (doğrulanamadı)")
    extra = [r for r in res["in_universe"] if r["symbol"] not in {p["symbol"] for p in res["peers"]}]
    if extra:
        print("Radar evreninde benzerler: " + ", ".join(f"{r['symbol']} ({r['why']})" for r in extra))
    for t in res["tried"]:
        print(f"  denendi: {t}")
    if res["next"]:
        print(f"Sonraki adım: {res['next']}")
    return 0


def register(sub) -> None:
    k = sub.add_parser("karsilastir", help="akran değerleme karşılaştırması: ilk sembol hedef, diğerleri akran (ucuz/makul/pahalı)")
    k.add_argument("symbols", nargs="+", metavar="SYM")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_karsilastir)

    p = sub.add_parser("peers", help="akran önerisi: FMP stock-peers → radar evreninde aynı endüstri → sabit liste")
    p.add_argument("symbol")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_peers)
