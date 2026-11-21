"""SEC EDGAR filings: 8-K (material events), 10-Q/10-K, offerings, 13D/G and Form 4 insider trades."""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx

from .. import config, db
from ..util import iso, parse_dt, sha, strip_html, utcnow

log = logging.getLogger(__name__)
_H = {"User-Agent": config.SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_lock = threading.Lock()
_last_call = 0.0
_cik_map: dict[str, int] = {}
_cik_loaded = 0.0

ITEMS_8K = {
    "1.01": ("Önemli anlaşma imzalandı", 3),
    "1.02": ("Önemli anlaşma feshedildi", 3),
    "1.03": ("İflas / kayyum", 5),
    "1.05": ("Siber güvenlik olayı", 4),
    "2.01": ("Satın alma / varlık satışı tamamlandı", 4),
    "2.02": ("Finansal sonuçlar (bilanço)", 4),
    "2.03": ("Yeni borçlanma", 3),
    "2.04": ("Borç hızlandırma tetiklendi", 4),
    "2.05": ("Yeniden yapılanma / çıkış maliyetleri", 4),
    "2.06": ("Değer düşüklüğü", 4),
    "3.01": ("Borsa kotasyon uyarısı", 4),
    "3.02": ("Kayıtsız hisse satışı", 3),
    "3.03": ("Hissedar haklarında değişiklik", 3),
    "4.01": ("Denetçi değişikliği", 3),
    "4.02": ("Geçmiş finansallar güvenilmez", 5),
    "5.01": ("Kontrol değişikliği", 4),
    "5.02": ("Yönetici / yönetim kurulu değişikliği", 3),
    "5.03": ("Esas sözleşme değişikliği", 2),
    "5.07": ("Genel kurul oylama sonuçları", 2),
    "7.01": ("Kamuya açıklama (Reg FD)", 2),
    "8.01": ("Diğer önemli olaylar", 2),
    "9.01": ("Finansal tablolar ve ekler", 1),
}
FORMS = {
    "8-K": 2, "8-K/A": 2, "10-Q": 2, "10-K": 3, "S-1": 3, "S-3": 3, "S-3ASR": 3,
    "424B1": 4, "424B2": 3, "424B3": 3, "424B4": 4, "424B5": 4, "424B7": 3,
    "SC 13D": 4, "SC 13D/A": 3, "SC 13G": 2, "SC 13G/A": 2, "SCHEDULE 13D": 4, "SCHEDULE 13G": 2,
    "6-K": 2, "4": 2,
}
FORM_TR = {
    "10-Q": "Çeyreklik rapor (10-Q)", "10-K": "Yıllık rapor (10-K)", "S-1": "Halka arz kaydı (S-1)",
    "S-3": "Hisse/borç ihracı kaydı (S-3)", "S-3ASR": "Otomatik raf kaydı (S-3ASR)",
    "SC 13D": "Aktivist / %5+ pay bildirimi (13D)", "SC 13G": "Pasif %5+ pay bildirimi (13G)",
    "SCHEDULE 13D": "Aktivist / %5+ pay bildirimi (13D)", "SCHEDULE 13G": "Pasif %5+ pay bildirimi (13G)",
}
KEEP_DAYS = 45
FRESH_HOURS = 48


def _get(url: str, **kw) -> httpx.Response:
    """SEC fair-access: <=10 req/s; we stay well below."""
    global _last_call
    with _lock:
        wait = 0.15 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    r = httpx.get(url, headers=_H, timeout=30, follow_redirects=True, **kw)
    r.raise_for_status()
    return r


def cik_for(symbol: str) -> int | None:
    global _cik_loaded
    if not _cik_map or time.time() - _cik_loaded > 86400:
        try:
            data = _get("https://www.sec.gov/files/company_tickers.json").json()
            _cik_map.clear()
            _cik_map.update({v["ticker"].upper(): int(v["cik_str"]) for v in data.values()})
            _cik_loaded = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("SEC ticker map failed: %s", exc)
    return _cik_map.get(symbol.upper())


def _doc_url(cik: int, accession: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{doc}"


def _form4(cik: int, accession: str, doc: str) -> dict | None:
    raw = re.sub(r"^xslF345X\d+/", "", doc)
    try:
        root = ET.fromstring(_get(_doc_url(cik, accession, raw)).content)
    except Exception as exc:  # noqa: BLE001
        log.warning("form4 parse failed %s: %s", accession, exc)
        return None
    owner = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "?"
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    title = ""
    if rel is not None:
        title = rel.findtext("officerTitle") or ("Yönetim kurulu üyesi" if rel.findtext("isDirector") in ("1", "true") else "")
        if not title and rel.findtext("isTenPercentOwner") in ("1", "true"):
            title = "%10+ hissedar"
    buy = sell = 0.0
    buy_sh = sell_sh = 0.0
    plan = "10b5-1" in ET.tostring(root, encoding="unicode")
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = tx.findtext(".//transactionCoding/transactionCode")
        try:
            sh = float(tx.findtext(".//transactionAmounts/transactionShares/value") or 0)
            px = float(tx.findtext(".//transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        if code == "P":
            buy += sh * px
            buy_sh += sh
        elif code == "S":
            sell += sh * px
            sell_sh += sh
    if not buy and not sell:
        return None  # grants, option exercises, tax withholding: not interesting
    return {"owner": owner.title(), "role": title, "buy_usd": round(buy), "sell_usd": round(sell),
            "buy_shares": buy_sh, "sell_shares": sell_sh, "plan_10b5_1": plan}


def _form4_event(sym: str, f4: dict) -> tuple[str, int, int]:
    who = f"{f4['owner']}" + (f" ({f4['role']})" if f4["role"] else "")
    if f4["buy_usd"] >= f4["sell_usd"]:
        usd = f4["buy_usd"]
        imp = 4 if usd >= 100_000 else 3
        return f"{sym}: içeriden ALIM — {who}, {usd/1e6:.2f}M$ ({f4['buy_shares']:,.0f} hisse)", imp, 1
    usd = f4["sell_usd"]
    imp = 3 if usd >= 1_000_000 and not f4["plan_10b5_1"] else 2
    plan = " · 10b5-1 planlı" if f4["plan_10b5_1"] else ""
    return f"{sym}: içeriden SATIŞ — {who}, {usd/1e6:.2f}M${plan}", imp, -1


def _filing_text(url: str, limit: int = 6000) -> str:
    try:
        return strip_html(_get(url).text, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("filing text failed %s: %s", url, exc)
        return ""


def collect_ticker(ticker) -> int:
    sym = ticker["symbol"]
    cik = ticker["cik"] or cik_for(sym)
    if not cik:
        return 0
    if not ticker["cik"]:
        db.execute("UPDATE tickers SET cik = ? WHERE symbol = ?", (cik, sym))
    try:
        rec = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()["filings"]["recent"]
    except Exception as exc:  # noqa: BLE001
        log.warning("SEC submissions %s failed: %s", sym, exc)
        return 0

    backfill = ticker["backfilled_at"] is None
    now = utcnow()
    keep_cut, fresh_cut = now - timedelta(days=KEEP_DAYS), now - timedelta(hours=FRESH_HOURS)
    new = 0
    for i, form in enumerate(rec["form"]):
        if form not in FORMS:
            continue
        accepted = parse_dt(rec["acceptanceDateTime"][i]) or parse_dt(rec["filingDate"][i])
        if not accepted or accepted < keep_cut:
            continue
        accession = rec["accessionNumber"][i]
        key = sha("sec", accession)
        if db.query_one("SELECT 1 FROM events WHERE dedupe_key = ?", (key,)):
            continue
        doc = rec["primaryDocument"][i]
        url = _doc_url(cik, accession, doc)
        status = "backfill" if (backfill or accepted < fresh_cut) else "pending"
        extra = {"form": form, "accession": accession}
        body = None
        direction = None

        if form == "4":
            f4 = _form4(cik, accession, doc)
            if not f4:
                # remember it so we don't re-fetch every cycle
                db.insert_event(type="filing", source="SEC", symbol=sym, url=url, title=f"{sym}: Form 4 (alım/satım yok)",
                                published_at=iso(accepted), dedupe_key=key, status="irrelevant", importance=1, extra=extra)
                continue
            title, imp, direction = _form4_event(sym, f4)
            extra.update(f4)
            if status == "pending":
                status = "analyzed"  # rule-based, no LLM needed
        elif form.startswith("8-K"):
            items = [x.strip() for x in (rec.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
            labels = [ITEMS_8K.get(x, (x, 2)) for x in items]
            imp = max([lvl for _, lvl in labels if lvl > 1] or [FORMS[form]])
            title = f"{sym}: 8-K — " + (", ".join(lbl for lbl, lvl in labels if lvl > 1) or "Önemli olay bildirimi")
            extra["items"] = items
            if status == "pending":
                body = _filing_text(url)
        else:
            imp = FORMS[form]
            label = FORM_TR.get(form) or (f"İhraç izahnamesi ({form}) — olası hisse satışı/sulandırma" if form.startswith("424B") else form)
            title = f"{sym}: {label}"
            needs_llm = form.startswith(("424B", "S-1", "S-3", "SC 13D", "SCHEDULE 13D"))
            if status == "pending":
                if needs_llm:
                    body = _filing_text(url, 4000)
                else:
                    status = "analyzed"  # periodic reports / passive stakes: rule-based

        eid = db.insert_event(type="filing", source="SEC", symbol=sym, url=url, title=title, body=body,
                              published_at=iso(accepted), dedupe_key=key, status=status,
                              importance=imp, direction=direction, extra=extra)
        if eid and status in ("pending", "analyzed"):
            new += 1
    return new
