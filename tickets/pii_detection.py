import re

import phonenumbers

# Detection is pattern matching + checksum validation, not ML — no LLM call, no
# external API. Confidence is ranked by how mathematically certain each check can
# be: email/IBAN/card have a real checksum or well-defined format behind them;
# phone relies on a well-tested library but still guesses a region for numbers
# without a country code; account number and address have no universal format at
# all and are explicitly best-effort.

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
ACCOUNT_NUMBER_RE = re.compile(r"\b\d{8,17}\b")
POSTAL_CODE_RE = re.compile(r"\b\d{5}(-\d{4})?\b|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", re.IGNORECASE)
STREET_KEYWORDS_RE = re.compile(r"\b(street|st\.|avenue|ave\.|road|rd\.|lane|ln\.|drive|dr\.|blvd|boulevard|block|sector)\b", re.IGNORECASE)

PII_TYPES = [
    ("email", "Email"),
    ("phone", "Phone Number"),
    ("card", "Credit/Debit Card"),
    ("iban", "IBAN"),
    ("account_number", "Bank Account Number"),
    ("address", "Address"),
]
CONFIDENCE_CHOICES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]


def _mask(value, keep_start=2, keep_end=2):
    value = value.strip()
    if len(value) <= keep_start + keep_end:
        return "*" * len(value)
    return value[:keep_start] + "*" * (len(value) - keep_start - keep_end) + value[-keep_end:]


def _mask_email(value):
    local, _, domain = value.partition("@")
    masked_local = (local[0] + "***") if len(local) > 1 else "*"
    domain_parts = domain.split(".")
    masked_domain = ("***." + domain_parts[-1]) if len(domain_parts) > 1 else "***"
    return f"{masked_local}@{masked_domain}"


def luhn_valid(digits):
    """Standard Luhn checksum — the real validation behind credit/debit card
    numbers, not just a digit-count guess."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_valid(candidate):
    """Standard IBAN mod-97 checksum: move the first 4 characters to the end,
    convert letters to numbers (A=10..Z=35), the result mod 97 must equal 1."""
    candidate = candidate.upper()
    rearranged = candidate[4:] + candidate[:4]
    numeric = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric += ch
        elif ch.isalpha():
            numeric += str(ord(ch) - ord("A") + 10)
        else:
            return False
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


def detect_pii(text, include_phone=True):
    """Scans one string value for sensitive-data patterns. Returns a list of
    {pii_type, confidence, masked_preview} dicts — the raw matched value is never
    returned beyond what the masked preview intentionally reveals, since a findings
    report holding full card numbers/IBANs would itself be a new data-handling risk.

    include_phone defaults True (unchanged behavior for every existing caller —
    ticket scanning always wants it). logscan.scanner is the one caller that
    passes False: phonenumbers.PhoneNumberMatcher does real parsing and is by
    far the most expensive check here, and most log lines contain digits
    (timestamps, ports, IDs) that would trigger it on nearly every line at
    100GB scale. Email/card/IBAN stay on unconditionally regardless of this
    flag — they're cheap regex passes and the higher-value signal for a
    log-leak scenario anyway."""
    if not text or not isinstance(text, str):
        return []
    text = text.strip()
    if not text:
        return []

    findings = []

    for m in EMAIL_RE.finditer(text):
        findings.append({"pii_type": "email", "confidence": "high", "masked_preview": _mask_email(m.group())})

    for m in IBAN_RE.finditer(text):
        if iban_valid(m.group()):
            findings.append({"pii_type": "iban", "confidence": "high", "masked_preview": _mask(m.group(), 4, 2)})

    card_spans = set()
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            card_spans.add(m.span())
            findings.append({"pii_type": "card", "confidence": "high", "masked_preview": _mask(digits, 0, 4)})

    if include_phone and any(ch.isdigit() for ch in text):
        try:
            for match in phonenumbers.PhoneNumberMatcher(text, "US"):
                findings.append({"pii_type": "phone", "confidence": "medium", "masked_preview": _mask(match.raw_string, 2, 2)})
        except Exception:
            pass

    # Bank account number — no universal format, so this is deliberately the
    # noisiest detector: any standalone 8-17 digit run not already claimed by a
    # validated card number. Flagged low-confidence on purpose.
    for m in ACCOUNT_NUMBER_RE.finditer(text):
        if any(m.start() >= s and m.end() <= e for s, e in card_spans):
            continue
        findings.append({"pii_type": "account_number", "confidence": "low", "masked_preview": _mask(m.group(), 2, 2)})

    # Address — best-effort heuristic (postal code pattern + a street-type keyword
    # both present). A reliable version would need NER; not attempted here.
    if POSTAL_CODE_RE.search(text) and STREET_KEYWORDS_RE.search(text):
        findings.append({"pii_type": "address", "confidence": "low", "masked_preview": _mask(text, 4, 4)})

    return findings


def redact_pii(text, include_phone=True):
    """Returns `text` with every detected PII span replaced by a bracketed type
    label (e.g. "user@example.com" -> "[EMAIL]") — the raw value is gone
    entirely, not just masked in a separate preview the way detect_pii()'s
    findings are. Built for contexts where a whole line/text needs to be safe
    to display verbatim (e.g. a log pattern cluster's representative example),
    not just have individual matches flagged elsewhere. Reuses the exact same
    regex/checksum matching detect_pii() uses, so a value detect_pii() would
    flag is exactly a value this redacts — same detection, different output.

    Address detection is a whole-text heuristic (postal code + street keyword
    both present somewhere in the text), not a specific span — there's nothing
    to redact for it here beyond whatever email/card/iban/phone/account spans
    happen to also be present in the same text. Not a claim that address
    content elsewhere in the line gets scrubbed."""
    if not text or not isinstance(text, str):
        return text or ""

    spans = []  # (start, end, label) — appended in roughly detect_pii()'s own priority order

    for m in EMAIL_RE.finditer(text):
        spans.append((m.start(), m.end(), "[EMAIL]"))

    for m in IBAN_RE.finditer(text):
        if iban_valid(m.group()):
            spans.append((m.start(), m.end(), "[IBAN]"))

    card_spans = set()
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            card_spans.add(m.span())  # untrimmed — kept as-is for the account-number overlap check below
            # CARD_RE's repeated `\d[ -]?` group can pull a trailing space/dash
            # into the match itself (e.g. matching "4111111111111111 " with the
            # space) — harmless for detect_pii()'s masked_preview (built from
            # `digits` above, already stripped of separators) but would eat a
            # real space out of the redacted line here if not trimmed back off.
            end = m.end()
            while end > m.start() and text[end - 1] in " -":
                end -= 1
            spans.append((m.start(), end, "[CARD]"))

    if include_phone and any(ch.isdigit() for ch in text):
        try:
            for match in phonenumbers.PhoneNumberMatcher(text, "US"):
                spans.append((match.start, match.start + len(match.raw_string), "[PHONE]"))
        except Exception:
            pass

    for m in ACCOUNT_NUMBER_RE.finditer(text):
        if any(m.start() >= s and m.end() <= e for s, e in card_spans):
            continue
        spans.append((m.start(), m.end(), "[ACCOUNT]"))

    if not spans:
        return text

    # Stable sort by start position — for spans with the same start, the
    # priority order they were appended in above (email > iban > card > phone
    # > account) survives the sort. Overlapping spans after that just keep
    # whichever was accepted first and drop the rest, rather than risk a
    # mangled double-replace.
    spans.sort(key=lambda s: s[0])
    accepted = []
    last_end = -1
    for start, end, label in spans:
        if start < last_end:
            continue
        accepted.append((start, end, label))
        last_end = end

    result = []
    cursor = 0
    for start, end, label in accepted:
        result.append(text[cursor:start])
        result.append(label)
        cursor = end
    result.append(text[cursor:])
    return "".join(result)


# Columns mapped to one of these target fields are *expected* to hold a name or
# email — that's what the field is for. A finding there isn't a surprise leak the
# way the same pattern showing up in an unmapped column or a free-text field like
# description would be; see PIIFinding.context.
IDENTITY_TARGET_FIELDS = {"created_by", "assigned_to"}


def _column_context(col, column_mapping):
    mapped_target = next((field for field, source_col in column_mapping.items() if source_col == col), None)
    if mapped_target is None:
        return "unmapped_column"
    if mapped_target in IDENTITY_TARGET_FIELDS:
        return "identity_field"
    return "free_text"


def scan_dataframe_for_pii(batch, df):
    """Scans every column of the raw uploaded file — mapped or not, since an
    unmapped column is exactly the data nobody chose to import and therefore
    nobody's watching — and writes masked PIIFinding rows. Runs at upload time,
    before any Ticket row exists yet, so `ticket` stays null; `row_reference` uses
    the external_id column's raw value if that column happens to already be mapped
    at scan time, else the row's position in the file."""
    from tickets.models import PIIFinding

    external_id_col = batch.column_mapping.get("external_id")
    findings = []
    for idx, row in df.iterrows():
        row_num = idx + 2  # +1 for 0-index, +1 for header row
        row_ref = str(row.get(external_id_col, "")).strip() if external_id_col else ""
        if not row_ref:
            row_ref = f"row {row_num}"
        for col in df.columns:
            value = row.get(col)
            if not value or not isinstance(value, str):
                continue
            col_context = _column_context(col, batch.column_mapping)
            for f in detect_pii(value):
                findings.append(PIIFinding(
                    project=batch.project, upload_batch=batch, ticket=None,
                    source_column=col, row_reference=row_ref, context=col_context,
                    pii_type=f["pii_type"], confidence=f["confidence"], masked_preview=f["masked_preview"],
                ))

    PIIFinding.objects.filter(upload_batch=batch).delete()
    PIIFinding.objects.bulk_create(findings, batch_size=500)
    return len(findings)
