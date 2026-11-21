"""SQLite storage. One short-lived connection per operation (thread-safe with WAL)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from . import config
from .util import utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol          TEXT PRIMARY KEY,
    name            TEXT,
    short_name      TEXT,
    sector          TEXT,
    cik             INTEGER,
    active          INTEGER NOT NULL DEFAULT 1,
    alert_threshold INTEGER,            -- NULL = global default
    notes           TEXT,
    added_at        TEXT NOT NULL,
    backfilled_at   TEXT                -- NULL = first collection still pending (no alerts)
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,        -- news | rating | filing | macro | data
    source        TEXT NOT NULL,
    symbol        TEXT,                 -- primary symbol (NULL for macro)
    url           TEXT,
    title         TEXT NOT NULL,
    body          TEXT,
    published_at  TEXT NOT NULL,        -- UTC ISO
    created_at    TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL UNIQUE,
    title_key     TEXT,                 -- normalized title for near-dup detection
    status        TEXT NOT NULL,        -- pending | analyzed | backfill | duplicate | irrelevant
    importance    INTEGER,              -- 1..5 (rule-based or LLM)
    direction     INTEGER,              -- -1 / 0 / 1
    extra         TEXT                  -- JSON (rating details, filing items, data values...)
);
CREATE INDEX IF NOT EXISTS ix_events_pub ON events(published_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_sym ON events(symbol, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_status ON events(status);

CREATE TABLE IF NOT EXISTS event_symbols (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    symbol   TEXT NOT NULL,
    PRIMARY KEY (event_id, symbol)
);
CREATE INDEX IF NOT EXISTS ix_event_symbols_sym ON event_symbols(symbol);

CREATE TABLE IF NOT EXISTS analyses (
    event_id         INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    summary_tr       TEXT,
    reasoning_tr     TEXT,
    category         TEXT,
    direction        INTEGER,
    importance       INTEGER,
    portfolio_impact TEXT,              -- JSON {symbol: -1|0|1}
    model            TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_reactions (
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL,
    price_t0   REAL,
    chg_15m    REAL,
    chg_1h     REAL,
    chg_1d     REAL,
    done       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (event_id, symbol)
);

CREATE TABLE IF NOT EXISTS macro_calendar (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,          -- cpi | nfp | fomc | ...
    name        TEXT NOT NULL,
    release_at  TEXT NOT NULL,          -- UTC ISO
    importance  INTEGER NOT NULL,
    reminded    INTEGER NOT NULL DEFAULT 0,
    released    INTEGER NOT NULL DEFAULT 0,
    event_id    INTEGER,
    UNIQUE(key, release_at)
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER,
    kind       TEXT NOT NULL,           -- alert | reminder | digest | system
    text       TEXT NOT NULL,
    status     TEXT NOT NULL,           -- queued | sent | failed | dropped
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_notif_status ON notifications(status, created_at);

CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,           -- morning | evening
    day        TEXT NOT NULL,           -- local date
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, day)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    job        TEXT PRIMARY KEY,
    last_run   TEXT,
    last_ok    TEXT,
    last_error TEXT,
    runs       INTEGER NOT NULL DEFAULT 0,
    errors     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);

-- Deep research (Hermes + web search) for important events
CREATE TABLE IF NOT EXISTS research (
    event_id   INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,          -- queued | done | failed
    content    TEXT,                   -- Turkish findings (plain text)
    expectation TEXT,                  -- consensus vs actual (data releases)
    sources    TEXT,                   -- JSON [{title,url}]
    model      TEXT,
    created_at TEXT NOT NULL,
    done_at    TEXT,
    error      TEXT
);

-- Kalshi prediction markets
CREATE TABLE IF NOT EXISTS kalshi_markets (
    market_ticker TEXT PRIMARY KEY,
    event_ticker  TEXT NOT NULL,
    series        TEXT NOT NULL,
    event_title   TEXT,
    label         TEXT,
    prob          REAL,                -- 0..1
    close_time    TEXT,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kalshi_history (
    market_ticker TEXT NOT NULL,
    ts            TEXT NOT NULL,
    prob          REAL,
    PRIMARY KEY (market_ticker, ts)
);

-- SEC XBRL quarterly fundamentals
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol       TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    fy           INTEGER,
    fp           TEXT,                 -- Q1..Q4 / FY
    form         TEXT,
    accession    TEXT,
    filed        TEXT,
    revenue      REAL,
    gross_profit REAL,
    op_income    REAL,
    net_income   REAL,
    eps_diluted  REAL,
    op_cash_flow REAL,
    cash         REAL,
    shares_out   REAL,
    PRIMARY KEY (symbol, period_end)
);

-- FINRA short sale volume (daily) and short interest (bi-monthly)
CREATE TABLE IF NOT EXISTS short_volume (
    symbol    TEXT NOT NULL,
    day       TEXT NOT NULL,
    short_vol REAL,
    total_vol REAL,
    ratio     REAL,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS short_interest (
    symbol          TEXT NOT NULL,
    settlement_date TEXT NOT NULL,
    short_qty       REAL,
    prev_qty        REAL,
    change_pct      REAL,
    days_to_cover   REAL,
    avg_daily_vol   REAL,
    PRIMARY KEY (symbol, settlement_date)
);

-- Reddit mentions (ApeWisdom)
CREATE TABLE IF NOT EXISTS social_mentions (
    symbol       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    rank         INTEGER,
    mentions     INTEGER,
    mentions_24h INTEGER,
    upvotes      INTEGER,
    PRIMARY KEY (symbol, ts)
);

-- generic per-source cursors (last seen ids / dates)
CREATE TABLE IF NOT EXISTS cursors (
    name  TEXT PRIMARY KEY,
    value TEXT
);

-- Daily bars shared by every engine (portfolio, regime, rotation, setups).
-- close = split-adjusted, not dividend-adjusted (Yahoo "Close").
CREATE TABLE IF NOT EXISTS prices_daily (
    symbol     TEXT NOT NULL,
    day        TEXT NOT NULL,          -- YYYY-MM-DD (exchange date)
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    adj_close  REAL,
    volume     REAL,
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS splits (
    symbol TEXT NOT NULL,
    day    TEXT NOT NULL,
    ratio  REAL NOT NULL,              -- 4.0 = 4-for-1
    PRIMARY KEY (symbol, day)
);

-- Personal portfolio ledger
CREATE TABLE IF NOT EXISTS subportfolios (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,   -- P1, P2 ...
    name       TEXT NOT NULL,
    color      TEXT,
    sort       INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,         -- UTC ISO
    sub_id      INTEGER NOT NULL REFERENCES subportfolios(id),
    symbol      TEXT NOT NULL,         -- price symbol: AMZN, BTC-USD, AMZN271217C00270000
    asset       TEXT NOT NULL DEFAULT 'stock',   -- stock | etf | option | crypto
    side        TEXT NOT NULL,         -- BUY | SELL
    qty         REAL NOT NULL,
    price       REAL NOT NULL,         -- per unit, in `currency`
    fees        REAL NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'USD',
    multiplier  REAL NOT NULL DEFAULT 1,
    underlying  TEXT,
    opt_type    TEXT,                  -- C | P
    strike      REAL,
    expiry      TEXT,
    note        TEXT,
    source      TEXT NOT NULL,         -- manual | revolut_csv
    ext_id      TEXT UNIQUE,           -- import dedupe key
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_sym ON trades(symbol);
CREATE TABLE IF NOT EXISTS cash_flows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    sub_id     INTEGER NOT NULL REFERENCES subportfolios(id),
    kind       TEXT NOT NULL,          -- deposit | withdrawal | dividend | interest | fee | transfer_in | transfer_out | adjustment
    amount     REAL NOT NULL,          -- signed, in `currency`
    currency   TEXT NOT NULL DEFAULT 'USD',
    symbol     TEXT,
    note       TEXT,
    source     TEXT NOT NULL,
    ext_id     TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cash_ts ON cash_flows(ts);

-- Raw API responses (FMP / Yahoo) with fetch time; engines stamp their numbers with it
CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    body       TEXT NOT NULL
);

-- Daily at-the-money implied volatility (for IV rank once enough history exists)
CREATE TABLE IF NOT EXISTS iv_history (
    symbol TEXT NOT NULL,
    day    TEXT NOT NULL,
    atm_iv REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);

-- Stock scorecards (every number server-computed; summary sentence by model)
CREATE TABLE IF NOT EXISTS scorecards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    as_of         TEXT,                -- price day the card is based on
    price         REAL,                -- close at as_of (for the track record)
    overall       REAL,
    tone          TEXT,
    dims          TEXT NOT NULL,       -- JSON
    weights_ver   TEXT NOT NULL,
    summary       TEXT,
    summary_model TEXT
);
CREATE INDEX IF NOT EXISTS ix_scorecards_sym ON scorecards(symbol, created_at DESC);

-- Setup scanner: every card is stored and followed bar by bar (source = live | backtest)
CREATE TABLE IF NOT EXISTS setups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    day         TEXT NOT NULL,          -- signal close (base day)
    source      TEXT NOT NULL,
    rules_ver   TEXT NOT NULL,
    entry_lo    REAL NOT NULL, entry_hi REAL NOT NULL, stop REAL NOT NULL, t1 REAL NOT NULL, t2 REAL NOT NULL,
    rr          REAL, quality REAL,
    valid_until TEXT NOT NULL,
    status      TEXT NOT NULL,          -- waiting | triggered | t1 | t2 | stop | time | expired | invalid
    trig_day    TEXT, trig_px REAL, t1_day TEXT, exit_day TEXT, exit_px REAL,
    r_mult      REAL, mfe REAL, mae REAL,
    regime      TEXT,
    details     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT,
    UNIQUE(symbol, kind, day, source)
);
CREATE INDEX IF NOT EXISTS ix_setups_day ON setups(source, day DESC);
CREATE INDEX IF NOT EXISTS ix_setups_status ON setups(status);

CREATE TABLE IF NOT EXISTS setup_runs (
    day           TEXT PRIMARY KEY,
    ran_at        TEXT NOT NULL,
    universe_n    INTEGER,
    skipped       TEXT,                 -- JSON [{symbol, src, why}]
    summary       TEXT,
    summary_model TEXT
);

-- Thesis notebook: why I own it, what would prove me wrong, when to look again
CREATE TABLE IF NOT EXISTS theses (
    id            INTEGER PRIMARY KEY,
    symbol        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- idea | active | review | closed
    kind          TEXT,                              -- uzun vade | büyüme | momentum | dönüş | bilanço | spekülatif | temettü
    thesis        TEXT NOT NULL,                     -- why: 2-5 sentences in the user's words
    catalysts     TEXT,                              -- what should happen (free text)
    target        REAL,                              -- optional price target
    horizon_months INTEGER,
    conviction    INTEGER,                           -- 1..5
    review_days   INTEGER NOT NULL DEFAULT 30,
    next_review   TEXT,                              -- YYYY-MM-DD
    entry_price   REAL,                              -- avg cost when the thesis was written (for reference)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT,
    outcome       TEXT,                              -- JSON {realized, pct, days} when closed
    lessons       TEXT
);
CREATE INDEX IF NOT EXISTS ix_theses_symbol ON theses(symbol, status);

CREATE TABLE IF NOT EXISTS thesis_rules (
    id           INTEGER PRIMARY KEY,
    thesis_id    INTEGER NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,       -- price_below | price_above | loss_pct | karne_below | rev_growth_below | dilution_above | regime_below | deadline | manual
    param        TEXT,                -- number or date; free text for manual
    note         TEXT,
    status       TEXT NOT NULL DEFAULT 'unknown',   -- ok | triggered | unknown
    last_value   TEXT,
    checked_at   TEXT,
    triggered_at TEXT
);

CREATE TABLE IF NOT EXISTS thesis_notes (
    id         INTEGER PRIMARY KEY,
    thesis_id  INTEGER NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,         -- note | rule | event | status | review
    text       TEXT NOT NULL,
    event_id   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_thesis_notes ON thesis_notes(thesis_id, ts);

-- Price / level alerts (one-shot unless repeat=1); evaluated on daily closes and intraday quotes
CREATE TABLE IF NOT EXISTS price_alerts (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL,
    cond        TEXT NOT NULL,        -- close_below | close_above | below | above | day_move
    level       REAL NOT NULL,
    note        TEXT,
    repeat      INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    fired_at    TEXT,
    fired_value REAL,
    fired_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_price_alerts_active ON price_alerts(active, symbol);

-- S&P 500 breadth per trading day (computed from stored constituent bars)
CREATE TABLE IF NOT EXISTS breadth_daily (
    day        TEXT PRIMARY KEY,
    n          INTEGER NOT NULL,     -- constituents with data that day
    above50    REAL,                 -- % above 50-day average
    above200   REAL,                 -- % above 200-day average
    adv        INTEGER, dec INTEGER, -- advancers / decliners
    nh         INTEGER, nl INTEGER,  -- new 52-week highs / lows
    ad_line    REAL,                 -- cumulative advancers - decliners
    mcclellan  REAL,                 -- EMA19 - EMA39 of (adv - dec)
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Daily market regime snapshot (rules v1); backfilled from stored bars
CREATE TABLE IF NOT EXISTS regime_history (
    day           TEXT PRIMARY KEY,
    overall       REAL,
    label         TEXT,
    tone          TEXT,
    climate       REAL,               -- 0-10 slow
    weather       REAL,               -- -8..+8 fast
    comps         TEXT NOT NULL,      -- JSON
    rules_ver     TEXT NOT NULL,
    summary       TEXT,
    summary_model TEXT,
    created_at    TEXT NOT NULL
);

-- Verdict layer ("Karar"): one rule-based card per held stock per run + the portfolio-level conclusion
CREATE TABLE IF NOT EXISTS verdict_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    as_of         TEXT,                -- price day of the run
    body          TEXT NOT NULL,       -- JSON board without cards
    comment       TEXT,
    comment_model TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES verdict_runs(id) ON DELETE CASCADE,
    symbol        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    as_of         TEXT,                -- price day the card is based on
    price         REAL,                -- close at as_of (for the track record)
    stance        TEXT NOT NULL,       -- alarm | sorgula | izle | saglam | veri_yok
    pattern       TEXT,
    rules_ver     TEXT NOT NULL,
    fingerprint   TEXT,                -- hash of stance + signal keys (comment reuse)
    body          TEXT NOT NULL,       -- JSON card
    comment       TEXT,
    comment_model TEXT
);
CREATE INDEX IF NOT EXISTS ix_verdicts_sym ON verdicts(symbol, id);
CREATE INDEX IF NOT EXISTS ix_verdicts_run ON verdicts(run_id);

-- Strategy lab paper book: every live signal of a lab strategy, followed on real bars with the backtest's own rules
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    sig_day       TEXT NOT NULL,       -- signal close
    tier          TEXT,                -- liste | kesif
    rank          REAL,                -- lower = more stretched (taken first by the capped book)
    limit_px      REAL,                -- NULL = buy at the next open
    status        TEXT NOT NULL,       -- pending | open | closed | lapsed (limit not reached)
    in_book       INTEGER,             -- 1 = one of the BOOK_SLOTS positions of the paper portfolio
    entry_day     TEXT,
    entry_px      REAL,
    exit_day      TEXT,
    exit_px       REAL,
    exit_next     INTEGER,             -- 1 = exit rule fired at the last close: out at the next open
    ret           REAL,                -- % (closed: net of costs; open: unrealised)
    held          INTEGER,
    why           TEXT,                -- signal | time | stop
    last_px       REAL,
    last_day      TEXT,
    body          TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT,
    UNIQUE(strategy, symbol, sig_day)
);
CREATE INDEX IF NOT EXISTS ix_paper_status ON paper_trades(status, strategy);

-- Stock screener ("Hisse tarayıcı"): nightly snapshot, one JSON row per listed stock; saved filter sets
CREATE TABLE IF NOT EXISTS screener_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    day           TEXT NOT NULL,
    body          TEXT NOT NULL        -- JSON: counts, price day, sources
);
CREATE TABLE IF NOT EXISTS screener_rows (
    run_id        INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    body          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_screener_rows ON screener_rows(run_id);
CREATE TABLE IF NOT EXISTS screener_saved (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    query         TEXT NOT NULL,       -- the page's query string
    created_at    TEXT NOT NULL
);

-- Strategy lab stress tests: every distinct parameter set ever tried (raises the pass bar) and one journal line per run
CREATE TABLE IF NOT EXISTS lab_attempts (
    key           TEXT PRIMARY KEY,     -- lab.Spec.key
    first_seen    TEXT NOT NULL,
    source        TEXT
);
CREATE TABLE IF NOT EXISTS lab_journal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    rules_hash    TEXT NOT NULL,        -- hash of lab_stress.RULES (thresholds fixed in advance)
    code_hash     TEXT NOT NULL,        -- hash of lab.py + lab_stress.py (the backtester and the tests)
    attempts      INTEGER NOT NULL,
    body          TEXT NOT NULL         -- JSON: verdict per strategy, data date
);

-- Idea screener ("Fikir listesi"): rule-based lists from SEC filings of every US filer
CREATE TABLE IF NOT EXISTS idea_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    day           TEXT NOT NULL,
    body          TEXT NOT NULL        -- JSON: universe size, counts per screen
);
CREATE TABLE IF NOT EXISTS ideas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES idea_runs(id) ON DELETE CASCADE,
    day           TEXT NOT NULL,
    screen        TEXT NOT NULL,       -- kara_yakin | ilk_kar | hizlanan | iyilesen
    symbol        TEXT NOT NULL,
    rank          INTEGER,
    shown         INTEGER,             -- 1 = in the top list of its screen (counted in the live record)
    price         REAL,                -- close on the listing day
    score         REAL,
    body          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ideas_run ON ideas(run_id, screen, rank);
CREATE INDEX IF NOT EXISTS ix_ideas_sym ON ideas(symbol, screen, day);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = connect()
    try:
        yield c
    finally:
        c.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


# columns added after a table first shipped: (table, column, declaration)
MIGRATIONS = [
    ("analyses", "story", "TEXT"),              # story key shared by items about the same underlying event
    ("analyses", "new_info", "INTEGER"),        # 1 = adds material facts to its story
    ("notifications", "tg_message_id", "INTEGER"),
    ("notifications", "reason", "TEXT"),        # why an alert was dropped / how it was sent
    ("setups", "tier", "TEXT"),                 # 'liste' (own list) | 'kesif' (wide index universe); NULL = liste
    ("setups", "picked", "INTEGER"),            # discovery tier: 1 = one of the few shown on the page
    ("setup_runs", "wide", "TEXT"),             # JSON: what the discovery scan covered and found
]


def init() -> None:
    config.ensure_dirs()
    with conn() as c:
        c.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS ix_analyses_story ON analyses(story)")
        for k, v in config.DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if not c.execute("SELECT 1 FROM subportfolios").fetchone():
            for i, (code, name, color) in enumerate(config.DEFAULT_SUBPORTFOLIOS):
                c.execute("INSERT INTO subportfolios(code, name, color, sort, created_at) VALUES (?,?,?,?,?)",
                          (code, name, color, i, utcnow_iso()))


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with conn() as c:
        cur = c.execute(sql, tuple(params))
        return cur.lastrowid or cur.rowcount


# ── settings ──────────────────────────────────────────────

def get_setting(key: str) -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else config.DEFAULT_SETTINGS.get(key, "")


def get_int(key: str) -> int:
    try:
        return int(get_setting(key))
    except ValueError:
        return int(config.DEFAULT_SETTINGS[key])


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_settings() -> dict[str, str]:
    out = dict(config.DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in query("SELECT key, value FROM settings")})
    return out


# ── tickers ───────────────────────────────────────────────

def active_tickers() -> list[sqlite3.Row]:
    return query("SELECT * FROM tickers WHERE active = 1 ORDER BY symbol")


def all_tickers() -> list[sqlite3.Row]:
    return query("SELECT * FROM tickers ORDER BY active DESC, symbol")


def get_ticker(symbol: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM tickers WHERE symbol = ?", (symbol.upper(),))


# ── events ────────────────────────────────────────────────

def insert_event(
    *,
    type: str,
    source: str,
    title: str,
    published_at: str,
    dedupe_key: str,
    status: str,
    symbol: str | None = None,
    symbols: Iterable[str] = (),
    url: str | None = None,
    body: str | None = None,
    title_key: str | None = None,
    importance: int | None = None,
    direction: int | None = None,
    extra: dict | None = None,
) -> int | None:
    """Insert an event; returns new id, or None if dedupe_key already exists
    (in which case any new symbols are attached to the existing event)."""
    syms = {s.upper() for s in symbols if s}
    if symbol:
        syms.add(symbol.upper())
    with tx() as c:
        row = c.execute("SELECT id FROM events WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        if row:
            for s in syms:
                c.execute("INSERT OR IGNORE INTO event_symbols(event_id, symbol) VALUES (?, ?)", (row["id"], s))
            return None
        cur = c.execute(
            """INSERT INTO events(type, source, symbol, url, title, body, published_at, created_at,
                                  dedupe_key, title_key, status, importance, direction, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                type, source, symbol.upper() if symbol else None, url, title, body, published_at,
                utcnow_iso(), dedupe_key, title_key, status, importance, direction,
                json.dumps(extra, ensure_ascii=False) if extra else None,
            ),
        )
        eid = cur.lastrowid
        for s in syms:
            c.execute("INSERT OR IGNORE INTO event_symbols(event_id, symbol) VALUES (?, ?)", (eid, s))
        return eid


def event_symbols(event_id: int) -> list[str]:
    return [r["symbol"] for r in query("SELECT symbol FROM event_symbols WHERE event_id = ? ORDER BY symbol", (event_id,))]


def get_event(event_id: int) -> sqlite3.Row | None:
    return query_one(
        """SELECT e.*, a.summary_tr, a.reasoning_tr, a.category, a.portfolio_impact, a.model
           FROM events e LEFT JOIN analyses a ON a.event_id = e.id WHERE e.id = ?""",
        (event_id,),
    )


def record_job(job: str, ok: bool, error: str | None = None) -> None:
    now = utcnow_iso()
    execute(
        """INSERT INTO job_runs(job, last_run, last_ok, last_error, runs, errors)
           VALUES (?, ?, ?, ?, 1, ?)
           ON CONFLICT(job) DO UPDATE SET
             last_run = excluded.last_run,
             last_ok = COALESCE(excluded.last_ok, job_runs.last_ok),
             last_error = excluded.last_error,
             runs = job_runs.runs + 1,
             errors = job_runs.errors + excluded.errors""",
        (job, now, now if ok else None, None if ok else (error or "")[:2000], 0 if ok else 1),
    )


def get_cursor(name: str) -> str | None:
    row = query_one("SELECT value FROM cursors WHERE name = ?", (name,))
    return row["value"] if row else None


def set_cursor(name: str, value: str) -> None:
    execute("INSERT INTO cursors(name, value) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default
