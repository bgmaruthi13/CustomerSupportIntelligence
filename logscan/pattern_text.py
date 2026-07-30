"""Log-line normalization for pattern clustering — strips the variable parts of
a log line (timestamps, UUIDs, IPs, generic numbers) before it's vectorized, so
"conn from 10.0.0.5 timed out" and "conn from 10.0.0.7 timed out" collapse into
the same template instead of fragmenting into near-duplicate clusters the way
raw-text TF-IDF would. A first-pass heuristic covering the common cases, not a
claim of full log-format-aware template mining (e.g. the Drain algorithm) —
good enough to prove pattern discovery works; revisit if a specific log format
needs more.

Order matters: timestamp/UUID/IP patterns are matched before the generic number
pattern specifically because they're more specific — if the number pattern ran
first it would chew up (say) an IP address digit-by-digit into
"<NUM>.<NUM>.<NUM>.<NUM>" instead of collapsing it to one "<IP>" token.
"""

import datetime
import re

from django.utils import timezone as django_timezone

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_NUMBER_RE = re.compile(r"\b\d+\b")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_log_line(line):
    line = _TIMESTAMP_RE.sub("<TS>", line)
    line = _UUID_RE.sub("<UUID>", line)
    line = _IP_RE.sub("<IP>", line)
    line = _NUMBER_RE.sub("<NUM>", line)
    line = _WHITESPACE_RE.sub(" ", line).strip()
    return line


def extract_timestamp(line):
    """Best-effort ISO-8601 timestamp extraction from a raw log line, for
    logscan.correlation's cross-source time-window matching. Reuses the same
    _TIMESTAMP_RE normalize_log_line() strips out, so it shares that
    function's format-coverage caveat (ISO-8601 only — traditional syslog
    "Mon DD HH:MM:SS" or Apache "[DD/Mon/YYYY:HH:MM:SS +0000]" timestamps
    won't be found here either). Returns a timezone-aware datetime, or None
    if the line has no ISO-8601-shaped timestamp or it fails to parse.
    Timestamps with no explicit UTC offset are assumed to be in Django's
    configured TIME_ZONE, since that's what this project's own sample/real
    log lines are written in."""
    match = _TIMESTAMP_RE.search(line)
    if not match:
        return None
    text = match.group(0).replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if django_timezone.is_naive(parsed):
        parsed = django_timezone.make_aware(parsed)
    return parsed
