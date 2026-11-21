# Notes for Claude (and other coding agents)

This is a self-hosted personal investing terminal. Python 3.12, FastAPI + Jinja templates, SQLite (`data/radar.db`), APScheduler jobs inside the web process. The CLI is `borsa-radar` (`uv run borsa-radar --help`). The UI text is Turkish; code and comments are English.

## Setting it up for a user

1. `uv sync`
2. Create the secrets file from the template: `mkdir -p ~/.config/borsa-radar && cp .env.example ~/.config/borsa-radar/secrets.env && chmod 600 ~/.config/borsa-radar/secrets.env`. Ask the user for the values they have. Every key is optional except `SEC_USER_AGENT` (their name and contact e-mail, which SEC requires). Let the user type the keys and their e-mail into the file themselves. Do not echo keys back, and never put keys in the repository, in a URL or in a commit.
3. `uv run borsa-radar status` creates the database. `uv run borsa-radar serve` starts the UI on http://localhost:8500.

## Importing a user's trades

- Revolut statement: `uv run borsa-radar import revolut statement.csv`.
- Any other broker: convert their export to the broker-neutral CSV (`date,type,symbol,quantity,price,fees,amount,currency`; see README and `examples/trades_template.csv`, parser in `src/borsa_radar/importers/generic.py`), then `uv run borsa-radar import auto file.csv --dry-run`, show the user the counts, and import without `--dry-run` once they agree.
- Map the broker's tickers to Yahoo symbols (`BRK.B` → `BRK-B`; non-US listings need Yahoo suffixes such as `.DE`, `.L`). Keep fees as positive numbers.
- Re-importing is idempotent (stable row ids). To start over, delete `data/radar.db` (ask first: it holds everything).
- Treat the user's trade files and database as private. Do not upload them, paste them into issues or commit them (`data/`, `reports/` and `*.db` are git-ignored).

## Layout

- `src/borsa_radar/` — one module per feature (`portfolio.py`, `risk.py`, `screener.py`, `lab*.py`, `regime.py` …). CLI plug-ins are `cli_ext_<name>.py` (registered in `cli.py`), web routers are `web/routes_<name>.py` (registered in `web/app.py`), pages are `web/templates/*.html`.
- `collectors/` — external data sources. `stockdata.py` caches API responses in the `api_cache` table with their fetch time.
- `importers/` — Revolut and the broker-neutral CSV.
- `tests/` — `uv run pytest -q`. Tests use a temporary database; they never touch `data/`.

## Principles to keep

- The server computes every number; an LLM may only write short commentary around numbers it is given. Every number carries its source and date.
- Screens and strategies are rule outputs with their measured history, never buy or sell advice. Strategy tests are judged on data they were not tuned on; the lab rejects by default.
- Telegram stays quiet: few, grouped, no repeats. Do not add new push types without the user asking.
- Free data first. Paid APIs only if the user asks.
