"""Paths, secrets and runtime settings."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path(os.environ.get("BORSA_RADAR_HOME", Path(__file__).resolve().parents[2]))   # the repository folder
DATA_DIR = APP_DIR / "data"
REPORTS_DIR = APP_DIR / "reports"
BACKUP_DIR = APP_DIR / "backups"
DB_PATH = Path(os.environ.get("BORSA_RADAR_DB", DATA_DIR / "radar.db"))
SECRETS_FILE = Path(os.environ.get("BORSA_RADAR_SECRETS", Path.home() / ".config" / "borsa-radar" / "secrets.env"))
HERMES_ENV = Path.home() / ".hermes" / ".env"
HERMES_BIN = os.environ.get("HERMES_BIN", str(Path.home() / ".local" / "bin" / "hermes"))


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def secret(name: str) -> str | None:
    """Env var first, then the secrets file (default ~/.config/borsa-radar/secrets.env, see .env.example)."""
    return os.environ.get(name) or _read_env_file(SECRETS_FILE).get(name) or None


LOCAL_TZ = ZoneInfo(secret("LOCAL_TZ") or "Europe/Berlin")
NY_TZ = ZoneInfo("America/New_York")

HTTP_PORT = int(secret("BORSA_RADAR_PORT") or "8500")
PUBLIC_BASE_URL = secret("BORSA_RADAR_URL") or f"http://localhost:{HTTP_PORT}"     # used in Telegram links

# SEC asks automated clients to name themselves with a contact address: set SEC_USER_AGENT="Your Name you@example.com"
SEC_USER_AGENT = secret("SEC_USER_AGENT") or "stock-market-portfolio-management research (set SEC_USER_AGENT)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) BorsaRadar/0.1"

DEFAULT_TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]          # first-run watchlist; edit in the UI

# Market-wide instruments shown for macro reactions.
MARKET_INSTRUMENTS = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000",
    "^VIX": "VIX",
    "^TNX": "ABD 10Y faiz",
    "DX-Y.NYB": "Dolar endeksi",
    "CL=F": "Petrol (WTI)",
    "GC=F": "Altın",
}

# Portfolio: sub-portfolios seeded on first run (renamable in the UI) and benchmarks.
DEFAULT_SUBPORTFOLIOS = [("P1", "Yatırım", "#3fb950"), ("P2", "Trade", "#58a6ff")]
BENCHMARKS = {"^GSPC": "SPX", "^NDX": "NDX", "^IXIC": "IXIC"}
CALENDAR_SYMBOL = "SPY"  # its stored bars define the trading-day calendar

# Market regime / pulse / rotation universe (all via the shared daily bar store).
INDEX_ETFS = {"SPY": "S&P 500", "RSP": "S&P 500 eşit ağırlık", "QQQ": "Nasdaq 100", "QQQE": "Nasdaq 100 eşit ağırlık",
              "DIA": "Dow Jones 30", "IWM": "Russell 2000"}
SECTOR_ETFS = {"XLK": "Teknoloji", "XLF": "Finans", "XLE": "Enerji", "XLV": "Sağlık", "XLI": "Sanayi",
               "XLY": "İhtiyari tüketim", "XLP": "Temel tüketim", "XLU": "Kamu hizmetleri", "XLB": "Malzeme",
               "XLRE": "Gayrimenkul", "XLC": "İletişim"}
FACTOR_ETFS = {"MTUM": "Momentum", "QUAL": "Kalite", "VLUE": "Değer", "USMV": "Düşük oynaklık", "IWF": "Büyüme",
               "IWD": "Değer (R1000)", "SMH": "Yarı iletken", "ARKK": "Yenilikçi büyüme", "SPHB": "Yüksek beta",
               "SPLV": "Düşük volatilite"}
MACRO_PROXIES = {"^VIX": "VIX", "^VIX3M": "VIX 3 ay", "^VXN": "VXN (Nasdaq oynaklık)", "^TNX": "ABD 10Y faiz",
                 "^IRX": "ABD 3A faiz", "DX-Y.NYB": "Dolar endeksi", "GLD": "Altın", "USO": "Petrol",
                 "HYG": "Yüksek getirili tahvil", "LQD": "Yatırım yapılabilir tahvil", "TLT": "20+Y Hazine",
                 "BTC-USD": "Bitcoin"}
# FMP / Yahoo sector name → SPDR sector ETF
SECTOR_TO_ETF = {"Technology": "XLK", "Financial Services": "XLF", "Financials": "XLF", "Energy": "XLE",
                 "Healthcare": "XLV", "Health Care": "XLV", "Industrials": "XLI", "Consumer Cyclical": "XLY",
                 "Consumer Discretionary": "XLY", "Consumer Defensive": "XLP", "Consumer Staples": "XLP",
                 "Utilities": "XLU", "Basic Materials": "XLB", "Materials": "XLB", "Real Estate": "XLRE",
                 "Communication Services": "XLC"}


def market_universe() -> list[str]:
    return list(INDEX_ETFS) + list(SECTOR_ETFS) + list(FACTOR_ETFS) + list(MACRO_PROXIES)

# Settings stored in DB (editable from the UI); these are defaults.
DEFAULT_SETTINGS = {
    "quiet_start": "00:00",
    "quiet_end": "08:00",
    "digest_morning": "15:00",
    "digest_evening": "22:30",
    "threshold_stock": "4",
    "threshold_macro": "4",
    "rating_pt_change_pct": "10",
    "rate_limit_per_10min": "6",
    "telegram_enabled": "1",
    "llm_provider": "anthropic",
    "llm_model": "claude-sonnet-5-5",
    "llm_fallback_provider": "openai-codex",
    "llm_fallback_model": "gpt-6-luna",
    "llm_daily_cap": "200",
    "research_daily_cap": "15",
    # setup scanner: portfolio + watchlist are always scanned; this is the extra pool (space separated)
    "setup_extra": "AAPL MSFT AMZN GOOGL META TSLA AMD AVGO NFLX PLTR JPM COST LLY UNH XOM QQQ IWM SMH",
    # discovery tier: index lists scanned on top of the user's own list (sp500 sp400 sp600; empty = off)
    "setup_wide": "sp500 sp400",
    "setup_alerts": "1",
    "watch_holdings": "1",
    "thesis_alerts": "1",
    "price_alerts": "1",
    "weekly_digest": "1",
    # risk per trade for the pre-trade check / position sizer (% of portfolio value)
    "risk_per_trade_pct": "1",
    "max_position_pct": "20",
    # verdict layer ("Karar"): one commentary call per run, tried before the default routes
    "karar_llm_provider": "anthropic",
    "karar_llm_model": "claude-opus-5-5",
}


def telegram_credentials() -> tuple[str | None, str | None]:
    """Reuse the Hermes Telegram bot (token + home chat) unless overridden."""
    env = _read_env_file(HERMES_ENV)
    token = secret("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat = (
        secret("TELEGRAM_CHAT_ID")
        or env.get("TELEGRAM_HOME_CHANNEL")
        or (env.get("TELEGRAM_ALLOWED_USERS", "").split(",")[0] or None)
    )
    return token, chat


def ensure_dirs() -> None:
    for d in (DATA_DIR, REPORTS_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
