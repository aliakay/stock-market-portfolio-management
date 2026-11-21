"""LLM calls through the local Hermes CLI (reuses the user's subscriptions / fallbacks)."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db
from .util import iso, utcnow

log = logging.getLogger(__name__)

# Dedicated empty cwd: no AGENTS.md is picked up, and sessions can be pruned by cwd.
LLM_CWD = config.DATA_DIR / "llm-cwd"
_sem = threading.Semaphore(2)


class LLMError(RuntimeError):
    pass


def _bump_usage(n: int = 1) -> int:
    day = datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO llm_usage(day, calls) VALUES (?, MAX(?, 0)) ON CONFLICT(day) DO UPDATE SET calls = MAX(calls + ?, 0)",
        (day, n, n),
    )
    row = db.query_one("SELECT calls FROM llm_usage WHERE day = ?", (day,))
    return row["calls"] if row else 0


def usage_today() -> int:
    day = datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d")
    row = db.query_one("SELECT calls FROM llm_usage WHERE day = ?", (day,))
    return row["calls"] if row else 0


def _run(prompt: str, provider: str, model: str, timeout: int, toolsets: str = "clarify") -> str:
    if not hermes_available():          # no agent CLI installed: LLM text is skipped, every number still works
        raise LLMError(f"LLM kapalı: {config.HERMES_BIN} bulunamadı (HERMES_BIN ile ayarlanır)")
    LLM_CWD.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as usage:
        cmd = [
            config.HERMES_BIN, "-z", prompt,
            "--provider", provider, "-m", model,
            "-t", toolsets, "--ignore-rules",
            "--usage-file", usage.name,
        ]
        proc = subprocess.run(cmd, cwd=LLM_CWD, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        raise LLMError(f"hermes exit={proc.returncode}: {(proc.stderr or out)[-400:]}")
    return out


RESERVE = 15  # calls kept free for priority work (digests) once routine analysis has used the rest
BURST = 25    # paced calls always allowed at the start of the day
AHEAD = 40    # "held" work (portfolio news) may run this many calls ahead of the paced allowance
# Relative weight of each Berlin hour when spreading the paced budget: US pre-market + session count most.
HOUR_WEIGHT = [1] * 8 + [2] * 6 + [4] * 9 + [2]  # 00-07, 08-13, 14-22, 23
_BAD_REPLY = ("No reply:", "Send `continue`", "didn't produce a reply")


COOLDOWN_MIN = 60  # a route whose provider reports its own quota / rate limit is skipped this long ...
COOLDOWN_MAX_MIN = 24 * 60  # ... or as long as the provider says ("retry after Ns"), capped at a day so it is re-checked daily
_RETRY_AFTER = re.compile(r"retry after (\d+)\s*s", re.I)
# The provider refused the call (nothing was used): don't charge it to the daily cap and don't hammer the route.
_REFUSED = re.compile(r"quota|rate[ -]?limit|too many requests|\b429\b|usage limit|insufficient (?:credit|quota|balance|funds)|billing", re.I)


class CapReached(LLMError):
    """Budget exhausted for now; the caller should keep its work queued and retry later."""


def _cool_key(provider: str, model: str) -> str:
    return f"llm_cool:{provider}:{model}"


def cooling(provider: str, model: str) -> bool:
    """True while a route sits out after its provider refused a call (shared by the service and the CLI via the DB)."""
    until = db.get_cursor(_cool_key(provider, model))
    return bool(until) and iso(utcnow()) < until


def paced_allowance(now: datetime | None = None) -> int:
    """How many calls 'paced' work may have used by now: BURST plus the day's budget released hour by hour."""
    now = now or datetime.now(config.LOCAL_TZ)
    budget = db.get_int("llm_daily_cap") - RESERVE
    done = sum(HOUR_WEIGHT[: now.hour]) + HOUR_WEIGHT[now.hour] * now.minute / 60
    return min(budget, int(BURST + (budget - BURST) * done / sum(HOUR_WEIGHT)))


def budget_left(budget: str = "paced") -> int:
    cap = db.get_int("llm_daily_cap")
    if budget == "held":
        limit = min(cap - RESERVE, paced_allowance() + AHEAD)
    else:
        limit = {"priority": cap, "daily": cap - RESERVE}.get(budget) or paced_allowance()
    return limit - usage_today()


def default_routes() -> list[tuple[str, str]]:
    return [
        (db.get_setting("llm_provider"), db.get_setting("llm_model")),
        (db.get_setting("llm_fallback_provider"), db.get_setting("llm_fallback_model")),
    ]


def complete(prompt: str, *, timeout: int = 240, toolsets: str = "clarify", priority: bool = False,
             budget: str = "paced", routes: list[tuple[str | None, str | None]] | None = None) -> tuple[str, str]:
    """Run a prompt; returns (text, model_label). Tries primary then fallback route.
    toolsets="web" lets Hermes search the web (slower, more tokens).
    budget: "paced" (default) follows the time-of-day allowance, "held" (portfolio news) may run AHEAD calls in
    front of it, "daily" may use the day's budget up to the digest reserve (manual requests),
    "priority" (= priority=True, digests) may use everything.
    routes: optional explicit [(provider, model), ...] tried in order instead of the default pair
    (use default_routes() to append the defaults); duplicates are tried once."""
    if budget_left("priority" if priority else budget) <= 0:
        raise CapReached("daily LLM cap reached" if priority or budget == "daily" else f"LLM budget paced ({budget}): wait")
    routes = [(p, m) for p, m in (list(dict.fromkeys(routes)) if routes else default_routes()) if p and m]
    live = [r for r in routes if not cooling(*r)]
    last: Exception | None = None
    with _sem:
        for provider, model in live or routes:  # every route cooling: try them anyway rather than fail without a call
            try:
                _bump_usage()
                out = _run(prompt, provider, model, timeout, toolsets)
                if not out.strip() or any(b in out for b in _BAD_REPLY):  # Hermes prints its own error text as a reply
                    raise LLMError(f"unusable reply: {out[:120]}")
                return out, f"{provider}:{model}"
            except (LLMError, subprocess.TimeoutExpired) as exc:
                log.warning("LLM route %s:%s failed: %s", provider, model, exc)
                last = exc
                if _REFUSED.search(str(exc)):
                    _bump_usage(-1)
                    m = _RETRY_AFTER.search(str(exc))
                    wait = min(COOLDOWN_MAX_MIN, max(COOLDOWN_MIN, int(m.group(1)) // 60)) if m else COOLDOWN_MIN
                    db.set_cursor(_cool_key(provider, model), iso(utcnow() + timedelta(minutes=wait)))
                if budget_left("priority" if priority else budget) <= 0:
                    raise CapReached(f"budget used up after failure: {exc}") from exc
    raise LLMError(f"all LLM routes failed: {last}")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str):
    """Extract the first JSON object/array from a model reply."""
    m = _FENCE.search(text)
    candidates = [m.group(1)] if m else []
    candidates.append(text)
    for c in candidates:
        c = c.strip()
        # try whichever bracket opens first, so an object containing a list isn't mistaken for the list
        pairs = sorted((("[", "]"), ("{", "}")), key=lambda p: (c.find(p[0]) == -1, c.find(p[0])))
        for opener, closer in pairs:
            i, j = c.find(opener), c.rfind(closer)
            if i != -1 and j > i:
                try:
                    return json.loads(c[i : j + 1])
                except ValueError:
                    continue
    raise LLMError(f"no JSON in reply: {text[:300]}")


def complete_json(prompt: str, **kw):
    text, model = complete(prompt, **kw)
    return parse_json(text), model


def prune_sessions() -> None:
    """Remove the one-shot sessions these calls leave in Hermes' session list."""
    try:
        subprocess.run(
            [config.HERMES_BIN, "sessions", "prune", "--cwd", str(LLM_CWD), "--yes"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("session prune failed: %s", exc)


def hermes_available() -> bool:
    return Path(config.HERMES_BIN).exists()
