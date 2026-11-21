"""Scheduled jobs and the scheduler."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from . import analyzer, config, db, digest, llm, notifier, reactions, research, tickers
from .collectors import fedreg, kalshi, macro, news, positioning, ratings, sec, usaspending, xbrl
from .util import us_extended_hours, utcnow_iso

log = logging.getLogger(__name__)
_collect_lock = threading.Lock()


def _job(name: str, fn):
    def run():
        try:
            fn()
            db.record_job(name, True)
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
            log.exception("job %s failed", name)
            db.record_job(name, False, repr(exc))
    run.__name__ = name
    return run


def collect_stocks() -> None:
    """News + SEC for every active ticker; marks first collection as backfill."""
    with _collect_lock:
        for t in db.active_tickers():
            news.collect_ticker(t)
            sec.collect_ticker(t)
            if t["backfilled_at"] is None:
                ratings.collect_ticker(t)
                db.execute("UPDATE tickers SET backfilled_at = ? WHERE symbol = ?", (utcnow_iso(), t["symbol"]))


def collect_ratings() -> None:
    for t in db.active_tickers():
        if t["backfilled_at"]:
            ratings.collect_ticker(t)


def collect_ratings_if_market() -> None:
    if us_extended_hours():
        collect_ratings()


def refresh_prices() -> None:
    """Daily bars for the portfolio, benchmarks and the watchlist."""
    from . import portfolio, prices
    try:
        tickers.sync_holdings()   # held stocks are watched too (news, SEC, alerts)
    except Exception:  # noqa: BLE001
        log.exception("sync_holdings failed")
    portfolio.refresh_prices()
    prices.refresh([t["symbol"] for t in db.active_tickers()] + config.market_universe())


def refresh_prices_if_market() -> None:
    if us_extended_hours():
        refresh_prices()


def market_regime() -> None:
    """After the close: S&P 500 constituent bars → breadth → regime snapshot (+ summary sentence)."""
    from . import breadth, regime
    breadth.refresh()
    regime.snapshot(use_llm=True)


def setup_scan() -> None:
    from . import setups
    setups.run_daily(use_llm=True)
    try:   # lab strategies use the bars the setup scan just refreshed
        from . import strateji
        strateji.daily_job()
    except Exception:  # noqa: BLE001
        log.exception("strateji daily job failed")


def ideas_weekly() -> None:
    """Saturday morning: refresh the SEC frames of the last quarters, rebuild the idea lists, re-measure the rules."""
    from . import ideas
    ideas.run()
    ideas.backtest()


def lab_audit() -> None:
    """First Sunday of the month: fresh 20-year bars, the stored backtest record and the four stress tests. Runs as a
    separate process (about 1 GB while it works; the web process keeps its memory)."""
    import subprocess
    import sys
    from pathlib import Path
    exe = Path(sys.executable).with_name("borsa-radar")
    r = subprocess.run([str(exe), "lab", "audit"], capture_output=True, text=True, timeout=4 * 3600)
    if r.returncode:
        raise RuntimeError(f"lab audit exit {r.returncode}: {r.stderr[-400:]}")


def screener_job() -> None:
    """After the US close: prices for every listed stock, the SEC numbers already on disk (Yahoo quarterly statements
    where SEC lacks recent quarters), the screener snapshot."""
    from . import screener
    screener.run()


def thesis_check() -> None:
    from . import thesis
    thesis.daily_job()


def alerts_live() -> None:
    from . import alerts
    if us_extended_hours():
        alerts.check_live()


def alerts_close() -> None:
    from . import alerts
    alerts.check_close()


def weekly_digest() -> None:
    try:
        from . import weekly
    except ModuleNotFoundError:
        return
    if db.get_setting("weekly_digest") != "0":
        weekly.send()


def earnings_warm() -> None:
    """Before the US open: refresh previews (option-implied moves) for holdings reporting soon, so post-mortems can compare."""
    from . import earnings
    earnings.upcoming(14, quick=False, held_only=True)


def scorecards() -> None:
    from . import scorecard
    scorecard.daily_job()


def karar_job() -> None:
    from . import karar
    karar.daily_job()


def process() -> None:
    """Analyze → reactions → alerts → research queue → send."""
    analyzer.analyze_pending()
    reactions.init_for_new_events()
    notifier.select_alerts()
    research.enqueue_candidates()
    notifier.flush()


def research_worker() -> None:
    """Deep research runs one at a time (slow: web search), separate from the fast pipeline."""
    for _ in range(3):
        if not research.run_one():
            break
    notifier.flush()


def backup() -> None:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.BACKUP_DIR / f"radar-{datetime.now(config.LOCAL_TZ):%Y%m%d}.db"
    src = sqlite3.connect(config.DB_PATH)
    try:
        out = sqlite3.connect(dest)
        src.backup(out)
        out.close()
    finally:
        src.close()
    for old in sorted(config.BACKUP_DIR.glob("radar-*.db"))[:-14]:
        old.unlink(missing_ok=True)


def run_all_once() -> None:
    """Full cycle (used by CLI `run` and on startup)."""
    macro.refresh_calendar()
    collect_stocks()
    collect_ratings()
    macro.collect_news()
    macro.check_releases()
    for fn in (kalshi.collect, fedreg.collect, news.collect_marketaux, positioning.collect_short_volume,
               positioning.collect_short_interest, positioning.collect_social, xbrl.refresh, usaspending.collect):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("startup %s failed", fn.__name__)
    process()


def build_scheduler() -> BackgroundScheduler:
    s = BackgroundScheduler(timezone=config.LOCAL_TZ, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    s.add_job(_job("collect_stocks", collect_stocks), "interval", minutes=5, id="collect_stocks")
    s.add_job(_job("collect_ratings_market", collect_ratings_if_market), "interval", minutes=15, id="collect_ratings_market")
    s.add_job(_job("collect_ratings", collect_ratings), "interval", hours=1, id="collect_ratings")
    s.add_job(_job("macro_news", macro.collect_news), "interval", minutes=5, id="macro_news")
    s.add_job(_job("macro_releases", macro.check_releases), "interval", minutes=2, id="macro_releases")
    s.add_job(_job("calendar", macro.refresh_calendar), "cron", hour="6,18", minute=5, id="calendar")
    s.add_job(_job("process", process), "interval", minutes=1, id="process")
    s.add_job(_job("reactions", reactions.update), "interval", minutes=10, id="reactions")
    s.add_job(_job("reminders", digest.reminders), "interval", minutes=5, id="reminders")
    s.add_job(_job("digests", digest.due_digests), "interval", minutes=5, id="digests")
    s.add_job(_job("research", research_worker), "interval", minutes=2, id="research")
    s.add_job(_job("kalshi", kalshi.collect), "interval", minutes=30, id="kalshi")
    s.add_job(_job("fedreg", fedreg.collect), "interval", hours=2, id="fedreg")
    s.add_job(_job("marketaux", news.collect_marketaux), "interval", minutes=20, id="marketaux")
    s.add_job(_job("short_volume", positioning.collect_short_volume), "cron", hour="1,3,5,9", minute=20, id="short_volume")
    s.add_job(_job("short_interest", positioning.collect_short_interest), "cron", hour=7, minute=10, id="short_interest")
    s.add_job(_job("social", positioning.collect_social), "interval", minutes=30, id="social")
    s.add_job(_job("xbrl", xbrl.refresh), "interval", hours=3, id="xbrl")
    s.add_job(_job("usaspending", usaspending.collect), "cron", hour=8, minute=40, id="usaspending")
    s.add_job(_job("prune_llm_sessions", llm.prune_sessions), "interval", minutes=30, id="prune_llm_sessions")
    s.add_job(_job("prices_market", refresh_prices_if_market), "interval", minutes=15, id="prices_market")
    s.add_job(_job("prices_close", refresh_prices), "cron", hour="0,7", minute=15, id="prices_close")
    s.add_job(_job("market_regime", market_regime), "cron", day_of_week="tue-sat", hour=0, minute=25, id="market_regime")
    s.add_job(_job("scorecards", scorecards), "cron", day_of_week="tue-sat", hour=0, minute=40, id="scorecards")
    s.add_job(_job("setup_scan", setup_scan), "cron", day_of_week="tue-sat", hour=1, minute=5, id="setup_scan")
    s.add_job(_job("thesis_check", thesis_check), "cron", day_of_week="tue-sat", hour=0, minute=55, id="thesis_check")
    s.add_job(_job("karar", karar_job), "cron", day_of_week="tue-sat", hour=1, minute=25, id="karar")
    s.add_job(_job("ideas", ideas_weekly), "cron", day_of_week="sat", hour=9, minute=30, id="ideas")
    s.add_job(_job("screener", screener_job), "cron", day_of_week="tue-sat", hour=2, minute=40, id="screener")
    # APScheduler combines day and day_of_week with AND: the first Sunday of every month
    s.add_job(_job("lab_audit", lab_audit), "cron", day="1-7", day_of_week="sun", hour=7, minute=0, id="lab_audit")
    s.add_job(_job("alerts_live", alerts_live), "interval", minutes=10, id="alerts_live")
    s.add_job(_job("alerts_close", alerts_close), "cron", hour="0,7", minute=25, id="alerts_close")
    s.add_job(_job("earnings_warm", earnings_warm), "cron", day_of_week="mon-fri", hour=14, minute=50, id="earnings_warm")
    s.add_job(_job("weekly_digest", weekly_digest), "cron", day_of_week="sun", hour=18, minute=0, id="weekly_digest")
    s.add_job(_job("backup", backup), "cron", hour=4, minute=30, id="backup")
    return s


def start_background() -> BackgroundScheduler:
    s = build_scheduler()
    s.start()
    # kick off an initial cycle without blocking web startup
    threading.Thread(target=_job("startup_cycle", run_all_once), daemon=True).start()
    return s


def disk_free_gb() -> float:
    return shutil.disk_usage(config.APP_DIR).free / 1e9
