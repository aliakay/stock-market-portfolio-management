# Stock Market Portfolio Management

A self-hosted personal investing terminal for US stocks: your portfolio and risk, a stock screener, market regime and sector rotation, a news and filings radar with optional Telegram alerts, and a backtest lab that rejects strategies by default.

It runs on your own machine. Your trades, keys and database never leave it. The web UI is in Turkish; the code, the CLI help and this README are in English. Feature-by-feature docs (Turkish): [docs/OZELLIKLER.md](docs/OZELLIKLER.md).

> **Not investment advice.** Everything here is measurement and research tooling. Lists, scores and backtests are rule outputs, not recommendations.

**Türkçe özet:** Kendi bilgisayarında çalışan kişisel yatırım terminali. İşlem geçmişini CSV ile yükle; portföy, risk, hisse tarayıcı, piyasa rejimi, sektör rotasyonu, haber radarı ve strateji laboratuvarı hazır. Arayüz Türkçe. Kurulum aşağıda; Claude Code kullanıyorsan repoyu klonlayıp Claude'a "kur ve işlem geçmişimi içe aktar" demen yeterli (bkz. [CLAUDE.md](CLAUDE.md)).

## What it does

| Area | Pages / commands |
|---|---|
| **Portfolio** | Ledger with sub-portfolios, average cost, realized P/L, time-weighted return vs S&P 500 / Nasdaq (`/portfolio`, `borsa-radar portfolio`) |
| **Import** | Revolut statements or a broker-neutral CSV (`borsa-radar import auto file.csv`, or upload on `/portfolio/trades`) |
| **Risk** | Beta, correlation clusters, VaR, stress tests (2024 yen unwind, 2025 tariff shock …), concentration warnings (`/risk`) |
| **Pre-trade check** | Position size from your risk budget, concentration, regime cap, thesis check (`borsa-radar kontrol buy NVDA --amount 1500`) |
| **Thesis notebook & alerts** | Why you hold it and what would prove you wrong; price alerts (`/tez`, `/alarmlar`) |
| **Stock screener** | ~4,000 US stocks, 30+ fields incl. beta and correlation with *your* portfolio, presets, saved screens, CSV (`/tarayici`, `borsa-radar tara`) |
| **Stock hub & scorecard** | Numbers, news, analysts, insiders, options map, fair value (`/h/NVDA`, `/h/NVDA/karne`) |
| **Market** | Regime, breadth, sector rotation, factors, macro calendar, Fed odds (`/piyasa`, `/calendar`) |
| **Idea lists** | Rule-based screens on SEC data (first profit, nearing profit …) with their measured historical edge (`/fikirler`) |
| **Strategy lab** | 20-year backtests, out-of-sample test, fee sweep, every-start-date test, random baselines; reject by default (`/setups/strateji`, `borsa-radar lab`) |
| **News radar** | Company news, analyst actions, SEC filings, macro releases; optional LLM summaries and Telegram alerts (`/`, `/feed`) |
| **Behavior & tax** | Trading-habit review, German capital-gains estimate (FIFO, EUR) (`/davranis`, `/vergi`) |

## Data sources

Every number shows its source and date in the UI. Free sources unless noted; keys go in your local secrets file (see [.env.example](.env.example)).

| Source | Used for | Key |
|---|---|---|
| [Yahoo Finance](https://finance.yahoo.com) via [yfinance](https://github.com/ranaroussi/yfinance) | Daily prices, quarterly statements, options chains, analyst estimates, news RSS | none |
| [SEC EDGAR](https://www.sec.gov/edgar) (XBRL frames, submissions, filings) | Fundamentals for ~5,000 companies, 8-K / 10-Q / Form 4 / 13D-G | none, but set `SEC_USER_AGENT` (name + e-mail) as SEC requires |
| [Finnhub](https://finnhub.io) | Company news, earnings calendar | free key |
| [Financial Modeling Prep](https://financialmodelingprep.com) | Company profiles, peers, ratings | free key (daily limit) |
| [Nasdaq Data Link / Sharadar](https://data.nasdaq.com/databases/SFA) | Free `tickers` table: listed and delisted US stocks, sectors (screener universe, survivorship-free backtests) | free key |
| [FRED](https://fred.stlouisfed.org) | US macro release calendar | free key |
| [Marketaux](https://www.marketaux.com) | Extra news feed | free key |
| [Kalshi](https://kalshi.com) | Prediction-market odds (Fed decisions, macro) | none |
| [FINRA](https://www.finra.org) | Short interest | none |
| [ApeWisdom](https://apewisdom.io) | Reddit mention counts | none |
| [USAspending](https://www.usaspending.gov) | Federal contracts by company | none |
| Fed, BLS, ECB, Federal Register, CNBC, Dow Jones RSS, Google News | Macro releases, central-bank statements, headlines | none |
| Wikipedia S&P 500/400/600 lists, [fja05680/sp500](https://github.com/fja05680/sp500) | Index members today and historically (point-in-time) | none |
| [Telegram Bot API](https://core.telegram.org/bots/api) | Optional alerts and digests | your bot token |
| LLM (optional) via an agent CLI | Short summaries of news and digests; numbers are always computed by the server | your own agent / API key |

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/aliakay/stock-market-portfolio-management.git
cd stock-market-portfolio-management
uv sync
mkdir -p ~/.config/borsa-radar
cp .env.example ~/.config/borsa-radar/secrets.env
chmod 600 ~/.config/borsa-radar/secrets.env      # then edit it: at least SEC_USER_AGENT
uv run borsa-radar status                        # creates data/radar.db
```

Start the web app (and the scheduled jobs) and open http://localhost:8500:

```bash
uv run borsa-radar serve
```

To keep it running, see [deploy/stock-market-portfolio-management.service.example](deploy/stock-market-portfolio-management.service.example). The CLI command keeps its original name, `borsa-radar`.

## Import your trading history

**Revolut:** app → Invest → ··· → Documents → Brokerage account → Account statement → Excel/CSV.

**Any other broker:** put your trades in a CSV with this header ([example](examples/trades_template.csv)):

```csv
date,type,symbol,quantity,price,fees,amount,currency
2025-01-02,DEPOSIT,,,,,5000,USD
2025-01-15,BUY,AAPL,10,185.20,1.00,,USD
2025-02-03,SELL,AAPL,4,192.10,1.00,,USD
2025-02-14,DIVIDEND,MSFT,,,,3.75,USD
```

- `type`: BUY, SELL, DEPOSIT, WITHDRAWAL, DIVIDEND, INTEREST, FEE.
- BUY / SELL need `symbol`, `quantity` and `price` (`fees` optional). Cash rows need `amount`; its sign does not matter.
- Symbols are Yahoo symbols (`BRK.B` becomes `BRK-B`). Dates: `2025-01-15`, `2025-01-15 15:30`, ISO, `15.01.2025`.
- Common header names from broker exports are recognised (`Trade Date`, `Action`, `Ticker`, `Shares`, `Commission` …).

Then:

```bash
uv run borsa-radar import auto my_trades.csv --dry-run   # check first, nothing is saved
uv run borsa-radar import auto my_trades.csv
uv run borsa-radar portfolio
```

Or upload the file on **Portföy → İşlemler** (`/portfolio/trades`). Importing the same file again is safe: rows already imported are skipped.

## Using it with Claude Code

Clone the repo, open it in [Claude Code](https://claude.com/claude-code) and ask, for example:

- "Set this up and import my trades from `~/Downloads/trades.csv`."
- "Convert my broker's export to the CSV format in the README, then import it."
- "What is my portfolio's beta and which holdings move together?" (`borsa-radar risk`)
- "Check a 1,500 $ buy of MSFT against my risk rules." (`borsa-radar kontrol buy MSFT --amount 1500`)

[CLAUDE.md](CLAUDE.md) tells Claude how the project is laid out and how to keep your data private. `borsa-radar context --json` gives an agent the whole picture (portfolio, risk, regime, alerts) in one call.

## Tests

```bash
uv run pytest -q
```

## Privacy

Your database (`data/`), reports, backups and secrets file are ignored by git. Nothing is sent anywhere except the data-source requests above (symbols only) and, if you enable them, Telegram messages to your own bot and LLM prompts to your own agent.

## License

MIT. Some features re-implement ideas from MIT-licensed projects such as [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) and [staskh/trading_skills](https://github.com/staskh/trading_skills).
