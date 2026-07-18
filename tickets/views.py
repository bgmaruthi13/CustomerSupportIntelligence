from collections import Counter

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Max, Value, When
from django.db.models.functions import (TruncDay, TruncMonth, TruncQuarter,
                                         TruncWeek)
from django.shortcuts import get_object_or_404, redirect, render

from core.utils import apply_sort, dumps_for_script, get_current_project
from tickets.ingestion import build_preview, commit_upload, guess_column_mapping, read_uploaded_file
from tickets.models import (ALL_TARGET_FIELDS, DATETIME_FORMAT_CHOICES,
                             MULTI_VALUE_DELIMITERS, OPTIONAL_FIELDS, PIIFinding,
                             RECOMMENDED_FIELDS, Ticket, UploadBatch)
from tickets.pii_detection import PII_TYPES, scan_dataframe_for_pii


UPLOAD_BATCHES_SORT_FIELDS = {
    "version": "version",
    "created": "created_at",
    "status": "status",
    "imported": "success_rows",
}


@login_required
def upload(request):
    project = get_current_project(request)
    if request.method == "POST" and project:
        file = request.FILES.get("file")
        if not file:
            messages.error(request, "Please choose a file to upload.")
            return redirect("tickets:upload")
        if not file.name.lower().endswith((".csv", ".xlsx", ".xls")):
            messages.error(request, "Unsupported file type — please upload a .csv or .xlsx file.")
            return redirect("tickets:upload")

        next_version = (UploadBatch.objects.filter(project=project).aggregate(Max("version"))["version__max"] or 0) + 1
        batch = UploadBatch.objects.create(project=project, file=file, original_filename=file.name, version=next_version)
        try:
            df = read_uploaded_file(batch.file)
        except Exception as exc:  # noqa: BLE001 — surface any parse failure to the user
            batch.status = "failed"
            batch.error_log = [{"row": 0, "reason": str(exc)}]
            batch.save()
            messages.error(request, f"Could not read file: {exc}")
            return redirect("tickets:upload")

        batch.detected_columns = list(df.columns)
        batch.column_mapping = guess_column_mapping(df.columns)
        batch.save()

        pii_count = scan_dataframe_for_pii(batch, df)
        if pii_count:
            messages.warning(request, f"This file appears to contain {pii_count} possible instance{'s' if pii_count != 1 else ''} of sensitive data (email/phone/card/etc.) — review the warning on the mapping page before importing.")

        return redirect("tickets:mapping", batch_id=batch.id)

    if project:
        recent_batches = apply_sort(
            request, UploadBatch.objects.filter(project=project),
            UPLOAD_BATCHES_SORT_FIELDS, default_field="created", default_dir="desc",
        )[:8]
    else:
        recent_batches = []
    return render(request, "tickets/upload.html", {"active_nav": "upload", "recent_batches": recent_batches})


@login_required
def mapping(request, batch_id):
    project = get_current_project(request)
    batch = get_object_or_404(UploadBatch, id=batch_id, project=project)
    df = read_uploaded_file(batch.file)
    # Captured before any POST handling mutates batch.status — this is what the
    # warning banner and confirm dialog need to know: is committing right now a
    # first-time import, or does it delete-and-replace tickets that already exist?
    is_recommit = batch.status == "committed"
    existing_ticket_count = Ticket.objects.filter(upload_batch=batch).count() if is_recommit else 0

    if request.method == "POST":
        new_mapping = {}
        for field in ALL_TARGET_FIELDS:
            source_col = request.POST.get(f"map_{field}", "")
            # Nothing is mandatory — every field's "include in analysis" checkbox is
            # user-controlled, so people can scope the import to just the columns they
            # actually want. Anything left out gets a sensible fallback at commit time
            # (see tickets.ingestion.commit_upload) instead of blocking the import.
            included = request.POST.get(f"include_{field}") == "on"
            if source_col and included:
                new_mapping[field] = source_col
        datetime_format = request.POST.get("datetime_format", "auto")
        batch.column_mapping = new_mapping
        batch.datetime_format = datetime_format
        batch.save()

        if request.POST.get("action") == "commit":
            commit_upload(batch)
            if is_recommit:
                messages.success(request, f"Re-imported {batch.success_rows} tickets with the updated mapping ({batch.error_rows} skipped, {len(batch.warning_log)} used a default). Re-run your pipelines to refresh clusters built from the old data.")
            else:
                messages.success(request, f"Imported {batch.success_rows} tickets ({batch.error_rows} skipped, {len(batch.warning_log)} used a default).")
            return redirect("tickets:upload_result", batch_id=batch.id)

    preview_rows = build_preview(df, batch.column_mapping, batch.datetime_format)

    pii_findings = PIIFinding.objects.filter(upload_batch=batch)
    pii_summary = []
    if pii_findings.exists():
        pii_type_labels = dict(PII_TYPES)
        mapped_columns = set(batch.column_mapping.values())
        by_type = Counter(pii_findings.values_list("pii_type", flat=True))
        for pii_type, count in by_type.most_common():
            columns = sorted(set(pii_findings.filter(pii_type=pii_type).values_list("source_column", flat=True)))
            pii_summary.append({
                "pii_type": pii_type,
                "label": pii_type_labels.get(pii_type, pii_type),
                "count": count,
                "columns": columns,
                "unmapped_columns": [c for c in columns if c not in mapped_columns],
            })

    return render(request, "tickets/mapping.html", {
        "active_nav": "upload",
        "batch": batch,
        "columns": batch.detected_columns,
        "recommended_fields": RECOMMENDED_FIELDS,
        "optional_fields": OPTIONAL_FIELDS,
        "all_fields": ALL_TARGET_FIELDS,
        "datetime_choices": DATETIME_FORMAT_CHOICES,
        "preview_rows": preview_rows,
        "row_count": len(df),
        "pii_summary": pii_summary,
        "pii_total": sum(s["count"] for s in pii_summary),
        "is_recommit": is_recommit,
        "existing_ticket_count": existing_ticket_count,
    })


@login_required
def upload_result(request, batch_id):
    project = get_current_project(request)
    batch = get_object_or_404(UploadBatch, id=batch_id, project=project)
    return render(request, "tickets/upload_result.html", {"active_nav": "upload", "batch": batch})


@login_required
def delete_batch(request, batch_id):
    project = get_current_project(request)
    batch = get_object_or_404(UploadBatch, id=batch_id, project=project)
    if request.method != "POST":
        return redirect("tickets:upload")

    deleted_count, _ = Ticket.objects.filter(upload_batch=batch).delete()
    filename = batch.original_filename
    batch.delete()
    messages.success(request, f'Deleted "{filename}" — removed {deleted_count} tickets. Re-run your pipelines to refresh clusters.')
    return redirect("tickets:upload")


@login_required
def delete_all_data(request):
    """Full reset: truncates every ticket/cluster/upload record for the project so it's
    back to a clean slate — including the upload history, so version numbering (Upload
    v1, v2, ...) restarts from v1 on the next file, not wherever it left off."""
    project = get_current_project(request)
    if request.method != "POST" or not project:
        return redirect("tickets:upload")

    if request.POST.get("confirm_name") != project.name:
        messages.error(request, "Project name didn't match — nothing was reset.")
        return redirect("tickets:upload")

    deleted_count, _ = Ticket.objects.filter(project=project).delete()
    from clustering.models import Cluster
    Cluster.objects.filter(project=project).delete()
    UploadBatch.objects.filter(project=project).delete()
    messages.success(request, f'Reset "{project.name}" — removed {deleted_count} tickets, all clusters, and upload history. Version numbering starts fresh at v1.')
    return redirect("tickets:upload")


UPLOADED_DATA_SORT_FIELDS = {
    "external_id": "external_id",
    "title": "title",
    "created": "created_at",
    "country": "country",
    "application": "application",
    "status": "status",
}
UPLOADED_DATA_PER_PAGE = 25


@login_required
def eda(request):
    project = get_current_project(request)
    context = {"active_nav": "eda", "project": project}
    if not project:
        return render(request, "tickets/eda.html", context)

    granularity = request.GET.get("granularity", "month")
    trunc_map = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth, "quarter": TruncQuarter}
    trunc_fn = trunc_map.get(granularity, TruncMonth)

    qs = Ticket.objects.filter(project=project)
    total = qs.count()

    volume_trend = (
        qs.annotate(bucket=trunc_fn("created_at"))
        .values("bucket")
        .annotate(count=Count("id"))
        .order_by("bucket")
    )
    by_country = qs.values("country").annotate(count=Count("id")).order_by("-count")[:12]
    by_application = qs.values("application").annotate(count=Count("id")).order_by("-count")[:12]
    by_status = qs.values("status").annotate(count=Count("id")).order_by("-count")
    by_priority = qs.values("priority").annotate(count=Count("id")).order_by("-count")
    by_queue = qs.values("queue").annotate(count=Count("id")).order_by("-count")[:12]
    by_requested_type = qs.values("requested_type").annotate(count=Count("id")).order_by("-count")[:12]

    # Offering is potentially multi-valued per ticket (delimiter-separated), so it's
    # exploded and tallied in Python rather than grouped in the DB — a straight
    # .values("offering") group-by would bucket "A, B" as one distinct combo instead
    # of counting toward both A and B.
    offering_counts = Counter()
    for offering in qs.exclude(offering="").values_list("offering", flat=True):
        for part in MULTI_VALUE_DELIMITERS.split(offering):
            part = part.strip()
            if part:
                offering_counts[part] += 1

    date_range = qs.order_by("created_at").first(), qs.order_by("-created_at").first()
    missing_country = qs.filter(country="Unspecified").count()
    missing_application = qs.filter(application="Unspecified").count()
    missing_queue = qs.filter(queue="Unspecified").count()
    missing_requested_type = qs.filter(requested_type="Unspecified").count()
    missing_description = qs.filter(description="").count()

    # Uploaded Data: already-imported Ticket rows, filterable by upload batch —
    # defaults to the most recent batch (fast, focused) rather than every ticket in
    # the project, with "All Uploads" available as an explicit filter choice.
    batches = UploadBatch.objects.filter(project=project).order_by("-created_at")
    batch_filter = request.GET.get("batch", "")
    if not batch_filter and batches:
        batch_filter = str(batches.first().id)
    data_qs = Ticket.objects.filter(project=project)
    selected_batch = None
    if batch_filter and batch_filter != "all":
        selected_batch = batches.filter(id=batch_filter).first()
        data_qs = data_qs.filter(upload_batch_id=batch_filter) if selected_batch else data_qs
    data_qs = apply_sort(request, data_qs, UPLOADED_DATA_SORT_FIELDS, default_field="created", default_dir="desc")
    data_paginator = Paginator(data_qs, UPLOADED_DATA_PER_PAGE)
    data_page = data_paginator.get_page(request.GET.get("page"))

    context.update({
        "total": total,
        "granularity": granularity,
        "volume_trend_json": dumps_for_script([
            {"label": row["bucket"].strftime("%Y-%m-%d") if row["bucket"] else "—", "count": row["count"]}
            for row in volume_trend
        ]),
        "by_country_json": dumps_for_script([{"label": r["country"] or "Unspecified", "count": r["count"]} for r in by_country]),
        "by_application_json": dumps_for_script([{"label": r["application"] or "Unspecified", "count": r["count"]} for r in by_application]),
        "by_status_json": dumps_for_script([{"label": r["status"] or "Unknown", "count": r["count"]} for r in by_status]),
        "by_priority_json": dumps_for_script([{"label": r["priority"] or "Unknown", "count": r["count"]} for r in by_priority]),
        "by_offering_json": dumps_for_script([{"label": k, "count": v} for k, v in offering_counts.most_common(12)]),
        "by_queue_json": dumps_for_script([{"label": r["queue"] or "Unspecified", "count": r["count"]} for r in by_queue]),
        "by_requested_type_json": dumps_for_script([{"label": r["requested_type"] or "Unspecified", "count": r["count"]} for r in by_requested_type]),
        "earliest": date_range[0].created_at if date_range[0] else None,
        "latest": date_range[1].created_at if date_range[1] else None,
        "missing_country": missing_country,
        "missing_application": missing_application,
        "missing_queue": missing_queue,
        "missing_requested_type": missing_requested_type,
        "missing_description": missing_description,
        "country_count": qs.values("country").distinct().count(),
        "application_count": qs.values("application").distinct().count(),
        "offering_count": len(offering_counts),
        "queue_count": qs.exclude(queue="Unspecified").values("queue").distinct().count(),
        "requested_type_count": qs.exclude(requested_type="Unspecified").values("requested_type").distinct().count(),
        "batches": batches,
        "batch_filter": batch_filter,
        "selected_batch": selected_batch,
        "data_page": data_page,
    })
    return render(request, "tickets/eda.html", context)


PII_REPORT_LIMIT = 200

# Lower rank = shown first by default. unmapped_column/free_text findings are
# the ones nobody chose to expose — those surface before identity_field
# findings (a name/email in a "Reported By" column is expected, not a leak).
PII_CONTEXT_RANK = Case(
    When(context="unmapped_column", then=Value(0)),
    When(context="free_text", then=Value(1)),
    default=Value(2),
    output_field=IntegerField(),
)

PII_SORT_FIELDS = {
    "risk": "context_rank",
    "type": "pii_type",
    "confidence": "confidence",
    "column": "source_column",
    "detected": "detected_at",
}


@login_required
def sensitive_data_report(request):
    """Standalone audit view across every upload in the project — the mapping
    page's warning is the pre-commit catch for one file; this is for reviewing
    what's already been imported. Masked previews only, filterable by
    type/confidence/context, defaulting to surfacing the findings most likely
    to matter (see PII_CONTEXT_RANK) rather than most-recently-detected."""
    project = get_current_project(request)
    context = {"active_nav": "pii-report", "project": project}
    if not project:
        return render(request, "tickets/pii_report.html", context)

    findings = PIIFinding.objects.filter(project=project).select_related("upload_batch", "ticket")

    type_filter = request.GET.get("type", "")
    confidence_filter = request.GET.get("confidence", "")
    context_filter = request.GET.get("context", "")
    if type_filter:
        findings = findings.filter(pii_type=type_filter)
    if confidence_filter:
        findings = findings.filter(confidence=confidence_filter)
    if context_filter:
        findings = findings.filter(context=context_filter)

    total_count = findings.count()
    by_type = Counter(PIIFinding.objects.filter(project=project).values_list("pii_type", flat=True))
    pii_type_labels = dict(PII_TYPES)
    context_labels = dict(PIIFinding._meta.get_field("context").choices)

    findings = findings.annotate(context_rank=PII_CONTEXT_RANK)
    findings = apply_sort(request, findings, PII_SORT_FIELDS, default_field="risk", default_dir="asc")

    context.update({
        "findings": findings[:PII_REPORT_LIMIT],
        "total_count": total_count,
        "shown_count": min(total_count, PII_REPORT_LIMIT),
        "type_counts": [{"value": t, "label": pii_type_labels.get(t, t), "count": c} for t, c in by_type.most_common()],
        "pii_types": PII_TYPES,
        "context_choices": PIIFinding._meta.get_field("context").choices,
        "context_labels": context_labels,
        "type_filter": type_filter,
        "confidence_filter": confidence_filter,
        "context_filter": context_filter,
    })
    return render(request, "tickets/pii_report.html", context)
