import re
from datetime import datetime

import pandas as pd
from dateutil import parser as dateutil_parser
from django.utils import timezone

from tickets.models import ALL_TARGET_FIELDS, Ticket
from tickets.pii_detection import scan_dataframe_for_pii

FIELD_KEYWORDS = {
    "external_id": ["incident id", "incident_id", "ticket id", "ticket_id", "id", "ref", "reference", "number"],
    "title": ["title", "summary", "subject", "short description"],
    "description": ["description", "details", "notes", "long description"],
    "created_at": ["created", "create", "date", "opened", "raised", "reported", "time"],
    "country": ["country", "region", "location", "geo"],
    "application": ["application", "app", "system", "product", "service"],
    "status": ["status", "state"],
    "priority": ["priority", "severity", "urgency"],
    "offering": ["offerings", "offering", "product offering", "service offering", "offer"],
    "requested_type": ["requested type", "request type", "requestedtype", "ticket type"],
    "assigned_to": ["assigned to", "assignedto", "assignee", "assigned"],
    "created_by": ["created by", "createdby", "creator", "author", "raised by"],
    "queue": ["queue", "assignment group", "assignmentgroup", "assigned group", "team"],
}

# Columns like "Created_By" / "AssignedBy" / "Resolved By" are actor/owner fields, not
# timestamps — without this, naive substring matching maps created_at to "CreatedBy"
# instead of "Created_Date"/"Create_Time", since "created" is literally a prefix of
# "createdby". This exclusion encodes a real ITSM naming convention, not a one-off hack.
ACTOR_SUFFIX_TOKENS = {"by", "user", "owner", "person"}


def _tokenize(col_name):
    """Splits a column name into lowercase whole words: "Create_Time" -> {"create","time"},
    "CreatedBy" -> {"created","by"}, "IncidentID" -> {"incident","id"}."""
    s = re.sub(r"[_\-]+", " ", col_name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    return {w.lower() for w in s.split() if w}


def read_uploaded_file(file_field):
    """Reads a Django FileField into a pandas DataFrame, handling CSV and Excel."""
    name = file_field.name.lower()
    file_field.seek(0)
    if name.endswith(".csv"):
        df = pd.read_csv(file_field, dtype=str, keep_default_na=False)
    elif name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_field, dtype=str)
        df = df.fillna("")
    else:
        raise ValueError("Unsupported file type — please upload a .csv or .xlsx file.")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def guess_column_mapping(columns):
    """Best-effort auto-mapping of source columns to target ticket fields.

    Matches on whole tokenized words (not raw substrings) — "created" must be an
    entire word in the column name, not just a prefix, so "CreatedBy" doesn't win
    over "Create_Time" for the created_at field just because "created" happens to
    prefix "createdby" as a string.
    """
    mapping = {}
    used_columns = set()
    tokenized = {col: _tokenize(col) for col in columns}

    for field in ALL_TARGET_FIELDS:
        keywords = FIELD_KEYWORDS.get(field, [field])
        best = None
        for col in columns:
            if col in used_columns:
                continue
            col_tokens = tokenized[col]
            if field == "created_at" and col_tokens & ACTOR_SUFFIX_TOKENS:
                continue  # e.g. "Created_By" — an actor field, not a timestamp
            for kw in keywords:
                kw_tokens = kw.split()
                if all(t in col_tokens for t in kw_tokens):
                    best = col
                    break
            if best:
                break
        if best:
            mapping[field] = best
            used_columns.add(best)
    return mapping


def parse_datetime_value(raw_value, fmt):
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None, "empty date value"
    try:
        if fmt == "auto":
            dt = dateutil_parser.parse(raw_value, dayfirst=False, fuzzy=True)
        else:
            dt = datetime.strptime(raw_value, fmt)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_default_timezone())
        return dt, None
    except (ValueError, OverflowError, TypeError) as exc:
        return None, f"unparseable date '{raw_value}' ({exc})"


def build_preview(df, mapping, datetime_format, limit=20):
    """Returns a small preview of mapped rows (for the mapping-confirmation screen)."""
    rows = []
    for _, row in df.head(limit).iterrows():
        parsed = {}
        for field in ALL_TARGET_FIELDS:
            src_col = mapping.get(field)
            if not src_col or src_col not in df.columns:
                parsed[field] = ""
                continue
            raw = str(row.get(src_col, "")).strip()
            if field == "created_at" and raw:
                dt, err = parse_datetime_value(raw, datetime_format)
                parsed[field] = dt.strftime("%Y-%m-%d %H:%M") if dt else f"ERR: {err}"
            else:
                parsed[field] = raw
        rows.append(parsed)
    return rows


def commit_upload(batch):
    """Parses the full uploaded file per the saved column mapping and bulk-creates Tickets.

    Nothing is mandatory to map — every field the user leaves unmapped (or that fails to
    parse) gets a sensible auto-generated fallback instead of blocking the row. The only
    thing that still skips a row is a genuine duplicate external_id, since silently
    merging two different tickets under one ID would corrupt the data rather than just
    be a missing-nicety.

    Re-committing an already-committed batch (editing its mapping and re-running,
    rather than a first-time commit) deletes and rebuilds this batch's own tickets
    first — otherwise every row would be skipped as a "duplicate external_id" against
    itself, silently no-op'ing the whole re-commit. Deleting cascades through
    ClusterMember/TicketEmbedding/DuplicateCandidate rows tied to those tickets; those
    go stale until the next pipeline run, same as any other data change.
    """
    df = read_uploaded_file(batch.file)
    mapping = batch.column_mapping
    fmt = batch.datetime_format

    if batch.status == "committed":
        Ticket.objects.filter(upload_batch=batch).delete()

    # Re-scan for PII against the mapping actually used for this commit — column
    # classification (identity_field/unmapped_column/free_text) depends on which
    # fields are mapped, so a changed mapping can genuinely change which findings
    # are real. scan_dataframe_for_pii already replaces this batch's prior findings
    # rather than appending, so this is safe to call on every commit, not just re-commits.
    scan_dataframe_for_pii(batch, df)

    errors = []    # rows skipped entirely (not imported)
    warnings = []  # rows imported using a fallback for an unmapped/unparseable field
    tickets_to_create = []
    seen_ids_in_batch = set()

    existing_ids = set(
        Ticket.objects.filter(project=batch.project).values_list("external_id", flat=True)
    )

    for idx, row in df.iterrows():
        row_num = idx + 2  # +1 for 0-index, +1 for header row
        row_notes = []
        values = {}

        for field in ALL_TARGET_FIELDS:
            src_col = mapping.get(field)
            raw = str(row.get(src_col, "")).strip() if src_col and src_col in df.columns else ""
            values[field] = raw

        created_at = None
        if values.get("created_at"):
            created_at, err = parse_datetime_value(values["created_at"], fmt)
            if err:
                row_notes.append(f"date defaulted to import time ({err})")
        else:
            row_notes.append("created_at not mapped — defaulted to import time")
        if created_at is None:
            created_at = timezone.now()

        title = values.get("title", "").strip()
        if not title:
            title = f"(untitled — row {row_num})"
            row_notes.append("title not mapped — placeholder used")

        external_id = values.get("external_id", "").strip()
        if not external_id:
            external_id = f"row-{batch.id}-{row_num}"
            row_notes.append("external_id not mapped — auto-generated")

        if external_id in existing_ids or external_id in seen_ids_in_batch:
            errors.append({"row": int(row_num), "reason": f"duplicate external_id '{external_id}' — row skipped"})
            continue

        seen_ids_in_batch.add(external_id)
        if row_notes:
            warnings.append({"row": int(row_num), "reason": "; ".join(row_notes)})

        tickets_to_create.append(Ticket(
            project=batch.project,
            upload_batch=batch,
            external_id=external_id,
            title=title[:500],
            description=values.get("description", ""),
            created_at=created_at,
            country=values.get("country") or "Unspecified",
            application=values.get("application") or "Unspecified",
            status=values.get("status") or "Open",
            priority=values.get("priority") or "Medium",
            offering=values.get("offering") or "Unspecified",
            requested_type=values.get("requested_type") or "Unspecified",
            assigned_to=values.get("assigned_to") or "Unassigned",
            created_by=values.get("created_by") or "Unknown",
            queue=values.get("queue") or "Unspecified",
            queue_source="mapped" if values.get("queue") else "",
        ))

    Ticket.objects.bulk_create(tickets_to_create, batch_size=500)

    batch.total_rows = len(df)
    batch.success_rows = len(tickets_to_create)
    batch.error_rows = len(errors)
    batch.error_log = errors[:500]
    batch.warning_log = warnings[:500]
    batch.status = "committed"
    batch.committed_at = timezone.now()
    batch.save()

    if tickets_to_create and batch.project.is_sample:
        batch.project.is_sample = False
        batch.project.save(update_fields=["is_sample"])

    return batch
