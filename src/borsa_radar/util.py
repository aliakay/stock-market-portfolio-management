"""Small helpers: time, hashing, text normalization, market hours."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "guccounter", "ncid", ".tsrc", "yptr"}
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "to", "of", "and", "in", "on", "for", "is", "are", "its", "it", "at", "as", "by", "with", "from"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return iso(utcnow())


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value) -> datetime | None:
    """Parse RFC822 / ISO strings / struct_time into aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "tm_year"):  # time.struct_time from feedparser (UTC)
        return datetime(*value[:6], tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_local(value: str | datetime | None) -> datetime | None:
    d = parse_dt(value) if not isinstance(value, datetime) else value
    return d.astimezone(config.LOCAL_TZ) if d else None


def fmt_local(value, fmt: str = "%d.%m %H:%M") -> str:
    d = to_local(value)
    return d.strftime(fmt) if d else ""


def sha(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(p or "" for p in parts).encode()).hexdigest()


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(q), ""))
    except ValueError:
        return url.strip()


def strip_source_suffix(title: str) -> str:
    """Google News appends ' - Publisher'."""
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip()


def title_tokens(title: str) -> set[str]:
    return {w for w in _WORD.findall(strip_source_suffix(title).lower()) if w not in _STOP}


def title_key(title: str) -> str:
    return " ".join(sorted(title_tokens(title)))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_TAG = re.compile(r"<[^>]+>")


def strip_html(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    t = _TAG.sub(" ", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] if limit else t


def hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime | None = None, start: str = "00:00", end: str = "08:00") -> bool:
    local = (now or utcnow()).astimezone(config.LOCAL_TZ).time()
    s, e = hhmm(start), hhmm(end)
    if s == e:
        return False
    return s <= local < e if s < e else (local >= s or local < e)


def us_market_open(now: datetime | None = None) -> bool:
    """Regular session 09:30-16:00 ET, Mon-Fri (holidays ignored)."""
    ny = (now or utcnow()).astimezone(config.NY_TZ)
    return ny.weekday() < 5 and time(9, 30) <= ny.time() < time(16, 0)


def us_extended_hours(now: datetime | None = None) -> bool:
    ny = (now or utcnow()).astimezone(config.NY_TZ)
    return ny.weekday() < 5 and time(4, 0) <= ny.time() < time(20, 0)


def ago(value) -> str:
    d = parse_dt(value)
    if not d:
        return ""
    s = int((utcnow() - d).total_seconds())
    if s < 60:
        return "şimdi"
    if s < 3600:
        return f"{s // 60} dk önce"
    if s < 86400:
        return f"{s // 3600} sa önce"
    return f"{s // 86400} gün önce"


def days_ago(n: float) -> str:
    return iso(utcnow() - timedelta(days=n))


# ── secrets never reach the logs ──────────────────────────

_SECRET_PARAM = re.compile(r"(?i)\b(\w*(?:key|token))=([^&\s'\"]+)")  # apikey, api_key, api_token, registrationkey, token…


def redact(text: str) -> str:
    """Mask API keys in URLs/query strings ('apikey=abc' → 'apikey=***')."""
    return _SECRET_PARAM.sub(r"\1=***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction() -> None:
    """Attach the filter to every root handler (handler filters see records from all loggers)."""
    for h in logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(RedactFilter())


def tr_n(v, digits: int = 2, sign: bool = False) -> str:
    """Turkish number format (1.234,56); '—' for missing."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return "—"
    s = f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")
