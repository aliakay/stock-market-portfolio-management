"""`borsa-radar lab [download | members | run | stress | audit | signals] [--universe pit|today]` — strateji laboratuvarı: 20 yıllık geriye dönük test ve kağıt işlem defteri."""

from __future__ import annotations

import json
import sys
import time

from .util import tr_n


def cmd_lab(args) -> int:
    from . import lab_data, strateji
    what = args.what or "paper"
    if what == "download":
        r = lab_data.download()
        print(f"{r['symbols']} sembol indirildi ({r['seconds']} sn); eksik: {', '.join(r['missing']) or 'yok'}")
        return 0
    if what == "members":
        m = lab_data.refresh_members()
        print(f"S&P 500 üyelik geçmişi {m['csv_through']}'e kadar · 2006'dan beri {m['names']} sembol · "
              f"eski üyeler için {m['download']['symbols']} hissenin fiyatı indirildi")
        for st, v in m["status"].items():
            print(f"  {st}: {v['names']} isim, üye-günlerin %{tr_n(v['share_days'], 1)}'i")
        return 0
    if what == "run":
        from . import lab_report
        t0 = time.time()
        res = lab_report.build(progress=lambda i, n: print(f"  {i}/{n}", file=sys.stderr, flush=True), universe=args.universe)
        print(f"sonuçlar kaydedildi ({time.time() - t0:.0f} sn) · evren: {res['universe_mode']} ({res['universe']} hisse)")
        for s in res["strategies"]:
            te = s["test"]
            print(f"  {s['name']}: test {te['n']} işlem, başarı %{tr_n(te['win'], 1)}, ort. %{tr_n(te['avg'], 2, True)}, "
                  f"kâr faktörü {tr_n(te['pf'], 2)}")
        return 0
    if what in ("stress", "audit"):
        from . import lab_report, lab_stress
        t0 = time.time()
        prog = lambda i, n: print(f"  {i}/{n}", file=sys.stderr, flush=True)  # noqa: E731
        if what == "audit":   # monthly: fresh bars → membership + former members → stored record → stress tests
            r = lab_data.download()
            print(f"{r['symbols']} sembol indirildi ({r['seconds']} sn)")
            try:
                m = lab_data.refresh_members()
                print(f"üyelik geçmişi yenilendi ({m['csv_through']}), eski üyelerden {m['download']['symbols']} hisse")
            except Exception as exc:  # noqa: BLE001 - the last good membership file stays in use
                print(f"üyelik geçmişi yenilenemedi: {exc}", file=sys.stderr)
            lab_report.build(progress=prog, universe=args.universe)
            print("geriye dönük test kaydı yenilendi")
        res = lab_stress.run(progress=prog, universe=args.universe)
        print(f"stres testleri bitti ({time.time() - t0:.0f} sn) · evren {res['universe_mode']} · denenen ayar {res['attempts']} · "
              f"z çıtası {tr_n(res['bar'], 2)}")
        for s in res["strategies"]:
            t = s["tests"]
            f, st, rk, rn = t["fee"], t["starts"], t["rank"], t["random"]
            print(f"  {s['name']}: {'GEÇTİ' if s['passed'] else 'REDDEDİLDİ (' + ', '.join(lab_stress.TEST_NAMES[c] for c in s['failed']) + ')'}")
            be = f["break_even"]
            print(f"    maliyet: başa baş %{tr_n(be, 2) if be is not None else '>2'} · %{tr_n(f['need_cost'], 2)} maliyette ort. "
                  f"%{tr_n(f['at_need']['avg'], 2, True)}, defter yıllık %{tr_n(f['at_need']['cagr'], 1, True)}")
            if st.get("n"):
                print(f"    başlangıç günleri ({st['n']}): 12 ay medyan %{tr_n(st['median'], 1, True)}, en kötü %{tr_n(st['worst'], 1, True)} "
                      f"({st['worst_day']}), S&P'yi geçen %{tr_n(st['share_beat_spy'], 0)}")
            print(f"    sıra: seçimde {rk['train_rank']}/{rk['train_of']}, sınavda {rk['test_rank']}/{rk['test_of']} (ilk çeyrek ≤ {rk['top_test']})")
            for lbl, key in (("aynı çıkış", "same_exit"), ("rastgele tutma", "random_hold")):
                d = rn.get(key)
                if d:
                    print(f"    rastgele ({lbl}): kural ort. %{tr_n(rn['real_avg'], 2, True)} · rastgele medyan %{tr_n(d['median'], 2, True)} · "
                          f"yüzdelik {tr_n(d['pct'], 1)} · z {tr_n(d['z'], 2) if d['z'] is not None else '—'}")
        return 0
    if what == "signals":
        r = strateji.daily_job()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    rec = strateji.paper_record()
    for st in strateji.STRATEGIES:
        a = rec[st.code]["all"]
        print(f"{st.name}: {a['n']} kapanan kağıt işlem" + (f", başarı %{tr_n(a['win'], 1)}, ort. %{tr_n(a['avg'], 2, True)}" if a["n"] else "")
              + f", açık {rec[st.code]['open']}")
    for t in strateji.signals():
        print(f"  {t['sig_day']} {t['strategy_name']}: {t['symbol']}" + (f" limit {tr_n(t['limit_px'], 2)}" if t.get("limit_px") else " açılışta")
              + (" [defter]" if t.get("in_book") else ""))
    return 0


def register(sub) -> None:
    p = sub.add_parser("lab", help="strateji laboratuvarı: lab download · lab members (S&P 500 üyelik geçmişi) · lab run (geriye dönük test) · lab stress (4 stres testi) · lab audit (aylık: indir + run + stress) · lab signals · lab (kağıt defter)")
    p.add_argument("what", nargs="?", help="download | members | run | stress | audit | signals | boşsa kağıt defter özeti")
    p.add_argument("--universe", choices=["pit", "today"], default="pit",
                   help="pit = o tarihteki S&P 500 üyeleri (varsayılan) · today = bugünkü S&P 500 + 400 (eski, hayatta kalanlar)")
    p.set_defaults(fn=cmd_lab)
