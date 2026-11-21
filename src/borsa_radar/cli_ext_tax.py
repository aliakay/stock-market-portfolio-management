"""CLI: `borsa-radar vergi [--year 2026] [--json] [--csv]` and `borsa-radar vergi fx-refresh`."""

from __future__ import annotations

import json
import sys

from .util import tr_n


def _eur(v, sign: bool = False) -> str:
    return "doğrulanamadı" if v is None else f"{tr_n(v, 2, sign=sign)} €"


def cmd_vergi(a) -> int:
    from . import tax
    if a.action == "fx-refresh":
        res = tax.fx_refresh()
        st = tax.fx_status()
        print(f"{'tamam' if res.get('ok') else 'BAŞARISIZ'} · kaynak {res.get('source') or '—'} · {res.get('rows', 0)} satır"
              + (f" · {res['error']}" if res.get("error") else ""))
        print(f"ECB USD: {st['ecb_first'] or '—'} → {st['ecb_last'] or '—'} ({st['ecb_rows']} gün) · Yahoo EURUSD=X son {st['yahoo_last'] or '—'}")
        return 0 if res.get("ok") else 1
    if a.csv:
        sys.stdout.write(tax.csv(a.year))
        return 0
    r = tax.report(a.year)
    if a.json:
        print(json.dumps(tax.brief(r) or {"year": r["year"], "empty": True}, ensure_ascii=False, indent=2, default=str))
        return 0
    if r.get("empty"):
        print(f"{r['year']} için veri yok (yıllar: {', '.join(map(str, r['years']))})")
        return 1
    print("! " + r["disclaimer"])
    print(r["summary"])
    st, p = r["steps"], r["pots"]
    print(f"Hisse potu: kâr {_eur(p['aktien']['gains'])} · zarar {_eur(p['aktien']['losses'])} · net {_eur(p['aktien']['net'], True)}"
          f" · önceki yıldan hisse zararı {_eur(st['carry_s_in'])} (kullanılan {_eur(st['used_s'])})")
    print(f"Diğer (sonstige): {_eur(st['other_total'], True)} · temettü brüt {_eur(r['div']['gross'])} (net {_eur(r['div']['net'])}, "
          f"stopaj {_eur(r['div']['wht'])}{' tahmini' if r['div']['assumed'] else ''}) · faiz {_eur(r['interest'])}")
    if p["fonds"]["n"]:
        print(f"Fon/ETF (KAP-INV): net {_eur(p['fonds']['net'], True)} · Teilfreistellung sonrası {_eur(p['fonds']['taxable'], True)}")
    if p["krypto"]["n"] or p["krypto_frei"]["n"]:
        c = r["crypto"]
        print(f"Kripto §23: ≤1 yıl net {_eur(c['net'], True)} · Freigrenze {_eur(c['freigrenze'])}"
              f"{' (altında → vergisiz)' if c['freigrenze_applied'] else ''} · vergiye tabi {_eur(c['taxable'])} · >1 yıl vergisiz {_eur(c['tax_free'], True)}")
    print(f"Sparer-Pauschbetrag {_eur(st['pauschbetrag'])} · vergiye tabi {_eur(st['taxable'])} · KapESt {_eur(st['kapest'])} "
          f"(stopaj mahsubu {_eur(st['credit'])}) + Soli {_eur(st['soli'])} = tahmini vergi {_eur(st['tax'])}")
    print(f"Devreden zarar → hisse {_eur(st['carry_s_out'])} · diğer {_eur(st['carry_o_out'])}")
    print(f"\nSatışlar ({len(r['sales'])}):")
    for s in r["sales"]:
        print(f"  {s['day']} {s['symbol']:<8} {s['qty']:>12.4f}  maliyet {_eur(s['cost_eur']):>14}  gelir {_eur(s['proceeds_eur']):>14}"
              f"  kazanç {_eur(s['gain_eur'], True):>14}  {s['hold_days']:.0f} gün  {s['pot_label']}")
    print(f"\nForm eşlemesi ({r['mapping_note']}):")
    for m in r["mapping"]:
        print(f"  {m['form']:<15} {m['field'][:80]:<80} {_eur(m['value'])}")
    for i in r["issues"]:
        print(("! " if i["level"] == "warn" else "· ") + i["text"])
    return 0


def register(sub) -> None:
    v = sub.add_parser("vergi", help="Almanya vergi tahmini: FIFO, ECB kuru ile EUR, Anlage KAP/KAP-INV/SO (vergi danışmanlığı değildir)")
    v.add_argument("action", nargs="?", choices=["rapor", "fx-refresh"], default="rapor",
                   help="rapor (varsayılan) veya fx-refresh: ECB referans kurlarını indir")
    v.add_argument("--year", "-y", type=int, help="vergi yılı (varsayılan: bu yıl)")
    v.add_argument("--json", action="store_true")
    v.add_argument("--csv", action="store_true", help="satış ve gelir satırları (';' ayraçlı, ondalık virgül)")
    v.set_defaults(fn=cmd_vergi)
