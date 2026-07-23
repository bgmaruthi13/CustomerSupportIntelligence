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

import re

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
