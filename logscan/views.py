import subprocess
import sys

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.export import csv_response
from core.utils import apply_sort, get_current_project
from logscan.correlation import correlate_project
from logscan.models import LogPatternCluster, LogPatternCorrelation, LogPIIFinding, LogScanJob, LogSource, SOURCE_TYPE_CHOICES, TRIGGER_MODE_CHOICES
from logscan.pattern_analysis import analyze_patterns
from tickets.pii_detection import PII_TYPES

SOURCES_SORT_FIELDS = {
    "name": "name",
    "created": "created_at",
    "trigger": "trigger_mode",
}
JOBS_SORT_FIELDS = {
    "created": "created_at",
    "status": "status",
    "findings": "findings_count",
}
FINDINGS_SORT_FIELDS = {
    "detected": "detected_at",
    "type": "pii_type",
    "confidence": "confidence",
    "line": "line_number",
}
FINDINGS_PER_PAGE = 50


@login_required
def sources(request):
    project = get_current_project(request)
    context = {"active_nav": "log-sources", "project": project}
    if not project:
        return render(request, "logscan/sources.html", context)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        source_type = request.POST.get("source_type", "path")
        trigger_mode = request.POST.get("trigger_mode", "on_demand")
        path = request.POST.get("path", "").strip()
        alert_emails = request.POST.get("alert_emails", "").strip()
        scan_phone_numbers = request.POST.get("scan_phone_numbers") == "on"
        uploaded_file = request.FILES.get("uploaded_file")
        file_pattern = request.POST.get("file_pattern", "*.log").strip() or "*.log"
        recursive = request.POST.get("recursive") == "on"

        if not name:
            messages.error(request, "Name is required.")
        elif source_type == "path" and not path:
            messages.error(request, "A filesystem/network path is required for this source type.")
        elif source_type == "upload" and not uploaded_file:
            messages.error(request, "Choose a file to upload for this source type.")
        elif source_type == "directory" and not path:
            messages.error(request, "A folder path is required for this source type.")
        else:
            LogSource.objects.create(
                project=project, name=name, source_type=source_type, path=path,
                uploaded_file=uploaded_file, trigger_mode=trigger_mode,
                scan_phone_numbers=scan_phone_numbers, alert_emails=alert_emails,
                file_pattern=file_pattern, recursive=recursive,
                created_by=request.user,
            )
            messages.success(request, f'Log source "{name}" added.')
            return redirect("logscan:sources")

    source_list = apply_sort(
        request, LogSource.objects.filter(project=project),
        SOURCES_SORT_FIELDS, default_field="created", default_dir="desc",
    )
    context.update({
        "sources": source_list,
        "source_type_choices": SOURCE_TYPE_CHOICES,
        "trigger_mode_choices": TRIGGER_MODE_CHOICES,
        "max_upload_mb": settings.LOGSCAN_UPLOAD_MAX_MEMORY_SIZE // (1024 * 1024),
    })
    return render(request, "logscan/sources.html", context)


@login_required
def delete_source(request, pk):
    project = get_current_project(request)
    source = get_object_or_404(LogSource, id=pk, project=project)
    if request.method != "POST":
        return redirect("logscan:sources")
    name = source.name
    source.delete()
    messages.success(request, f'Deleted log source "{name}" and its scan history.')
    return redirect("logscan:sources")


def _launch_scan_subprocess(job_id):
    """Launches `manage.py run_log_scan --job-id=<id>` fully detached from this
    request/process — a scan can run for a long time on a large file, and it
    must keep running independent of whether this HTTP response has already
    been sent, or whether the web server process itself later recycles."""
    manage_py = str(settings.BASE_DIR / "manage.py")
    args = [sys.executable, manage_py, "run_log_scan", f"--job-id={job_id}"]
    kwargs = {"cwd": str(settings.BASE_DIR), "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


@login_required
def scan_now(request, pk):
    project = get_current_project(request)
    source = get_object_or_404(LogSource, id=pk, project=project)
    if request.method != "POST":
        return redirect("logscan:sources")

    job = LogScanJob.objects.create(
        source=source, status="pending", triggered_by="manual",
        start_offset=source.last_scanned_offset,
    )
    _launch_scan_subprocess(job.id)
    messages.success(request, f'Scan started for "{source.name}" — resuming from byte {source.last_scanned_offset:,}.')
    return redirect("logscan:job_detail", pk=job.id)


@login_required
def job_detail(request, pk):
    project = get_current_project(request)
    job = get_object_or_404(LogScanJob, id=pk, source__project=project)
    return render(request, "logscan/job_detail.html", {"active_nav": "log-jobs", "project": project, "job": job})


@login_required
def job_status_json(request, pk):
    project = get_current_project(request)
    job = get_object_or_404(LogScanJob, id=pk, source__project=project)
    return JsonResponse({
        "status": job.status,
        "bytes_scanned": job.bytes_scanned,
        "bytes_total": job.bytes_total,
        "progress_pct": job.progress_pct,
        "lines_scanned": job.lines_scanned,
        "findings_count": job.findings_count,
        "error_message": job.error_message,
    })


@login_required
def jobs_list(request):
    project = get_current_project(request)
    context = {"active_nav": "log-jobs", "project": project}
    if not project:
        return render(request, "logscan/jobs.html", context)

    job_list = apply_sort(
        request, LogScanJob.objects.filter(source__project=project).select_related("source"),
        JOBS_SORT_FIELDS, default_field="created", default_dir="desc",
    )[:100]
    context["jobs"] = job_list
    return render(request, "logscan/jobs.html", context)


def _filtered_findings(request, project):
    """Shared by findings_report (paginated HTML) and findings_download (full
    CSV) so the two can never drift apart on what "the current filter" means —
    a download that silently applied different filters than what's on screen
    would be a worse bug than not having a download at all."""
    findings = LogPIIFinding.objects.filter(source__project=project).select_related("source", "job")
    type_filter = request.GET.get("type", "")
    confidence_filter = request.GET.get("confidence", "")
    source_filter = request.GET.get("source", "")
    if type_filter:
        findings = findings.filter(pii_type=type_filter)
    if confidence_filter:
        findings = findings.filter(confidence=confidence_filter)
    if source_filter:
        findings = findings.filter(source_id=source_filter)
    return findings, type_filter, confidence_filter, source_filter


@login_required
def findings_report(request):
    """Standalone report across every LogSource in the project — same shape as
    tickets.views.sensitive_data_report (masked previews only, filterable by
    type/confidence), for the same reason: this is for reviewing what's
    already been found, not the per-scan status page (job_detail)."""
    project = get_current_project(request)
    context = {"active_nav": "log-findings", "project": project}
    if not project:
        return render(request, "logscan/findings.html", context)

    findings, type_filter, confidence_filter, source_filter = _filtered_findings(request, project)

    total_count = findings.count()
    findings = apply_sort(request, findings, FINDINGS_SORT_FIELDS, default_field="detected", default_dir="desc")

    from django.core.paginator import Paginator
    page = Paginator(findings, FINDINGS_PER_PAGE).get_page(request.GET.get("page"))

    context.update({
        "page": page,
        "total_count": total_count,
        "pii_types": PII_TYPES,
        "sources": LogSource.objects.filter(project=project),
        "type_filter": type_filter,
        "confidence_filter": confidence_filter,
        "source_filter": source_filter,
    })
    return render(request, "logscan/findings.html", context)


@login_required
def findings_download(request):
    """CSV export of the currently-filtered Log PII Alerts view. Exports the
    exact same fields the page itself shows — pii_type, confidence,
    masked_preview, source, file_path, line_number, detected_at — and nothing
    else; masked_preview is what LogPIIFinding stores, the raw matched value
    was never persisted anywhere in this app to begin with (see the model's
    own docstring), so there is no raw value this export could leak even by
    mistake."""
    project = get_current_project(request)
    if not project:
        return redirect("logscan:findings_report")

    findings, _type_filter, _confidence_filter, _source_filter = _filtered_findings(request, project)
    findings = findings.order_by("-detected_at")

    rows = (
        (f.get_pii_type_display(), f.get_confidence_display(), f.masked_preview,
         f.source.name, f.file_path, f.line_number, f.detected_at.strftime("%Y-%m-%d %H:%M:%S"))
        for f in findings.iterator()
    )
    return csv_response(
        rows=rows,
        headers=["Type", "Confidence", "Masked Preview", "Source", "File", "Line", "Detected At"],
        filename=f"log_pii_alerts_project{project.id}",
    )


@login_required
def find_patterns(request, pk):
    """Triggers logscan.pattern_analysis.analyze_patterns synchronously — bounded
    by design (a recent slice of the file, not the whole thing), so unlike the
    PII scan trigger this doesn't need a detached subprocess or a status-polling
    page; it's done well within one request."""
    project = get_current_project(request)
    source = get_object_or_404(LogSource, id=pk, project=project)
    if request.method != "POST":
        return redirect("logscan:sources")

    if source.source_type == "directory":
        messages.error(request, "Pattern analysis isn't available for directory sources yet — pick a single-file (path or upload) source.")
        return redirect("logscan:sources")

    result = analyze_patterns(source)
    if result.ran:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect("logscan:patterns", pk=source.id)


@login_required
def patterns(request, pk):
    project = get_current_project(request)
    source = get_object_or_404(LogSource, id=pk, project=project)
    clusters = LogPatternCluster.objects.filter(source=source).order_by("-recurring_count")
    return render(request, "logscan/patterns.html", {
        "active_nav": "log-sources", "project": project, "source": source, "clusters": clusters,
    })


def _build_pattern_narrative_prompt(cluster):
    """BACKLOG A.6 — same idea as clustering's _build_summary_prompt, applied to
    a log pattern instead of a ticket cluster: turn keywords + one redacted
    example line into a plain-English 'what's likely happening' sentence.
    example_line is already redact_pii()'d before it was ever saved (see the
    model's own docstring) - nothing extra to sanitize here."""
    return "\n".join([
        "Here is a recurring pattern found in a log file (source: "
        f"'{cluster.source.name}'). In 2-3 plain-English sentences, explain what's "
        "likely happening and why it matters — reference the specific keywords/"
        "phrase in the example line below that led you there, not a generic "
        "restatement of the keyword list. The kind of explanation an on-call "
        "engineer would say out loud, not a log-parsing summary.",
        "",
        f"Pattern keywords: {cluster.keywords}",
        f"Recurring count: {cluster.recurring_count} of {cluster.lines_analyzed} recent lines analyzed",
        f"Example line (redacted): {cluster.example_line}",
    ])


@login_required
def generate_pattern_narrative(request, pk):
    from core.llm_bridge import LLMBridgeUnavailable, ask
    project = get_current_project(request)
    cluster = get_object_or_404(LogPatternCluster, id=pk, source__project=project)
    if request.method != "POST":
        return redirect("logscan:patterns", pk=cluster.source_id)
    try:
        cluster.ai_narrative = ask(_build_pattern_narrative_prompt(cluster))
        cluster.save(update_fields=["ai_narrative"])
        messages.success(request, "Narrative generated via the Copilot HTTP Bridge.")
    except LLMBridgeUnavailable as exc:
        messages.error(request, f"Couldn't generate a narrative: {exc}")
    return redirect("logscan:patterns", pk=cluster.source_id)


@login_required
def patterns_download(request, pk):
    """CSV of one source's Find Patterns results. example_line is already
    redact_pii()'d before it's ever saved to LogPatternCluster (see the
    model's own docstring), so this export carries the same never-raw
    guarantee as the on-screen table — no extra redaction step needed here."""
    project = get_current_project(request)
    source = get_object_or_404(LogSource, id=pk, project=project)
    clusters = LogPatternCluster.objects.filter(source=source).order_by("-recurring_count")

    rows = (
        (c.name, c.keywords, c.recurring_count, f"{c.confidence:.0f}", "Yes" if c.is_noise else "No",
         c.example_line, c.lines_analyzed, c.created_at.strftime("%Y-%m-%d %H:%M:%S"))
        for c in clusters.iterator()
    )
    return csv_response(
        rows=rows,
        headers=["Pattern", "Keywords", "Recurring Count", "Confidence %", "Is Noise", "Example (redacted)", "Lines Analyzed", "Created At"],
        filename=f"log_patterns_source{source.id}",
    )


@login_required
def run_correlation(request):
    """Triggers logscan.correlation.correlate_project synchronously — it only
    compares timestamps already sitting on existing LogPatternCluster rows
    (no file I/O, no re-clustering), so like find_patterns this is well
    within one request and needs no subprocess/status-polling."""
    project = get_current_project(request)
    if request.method != "POST":
        return redirect("logscan:correlations")
    if not project:
        return redirect("logscan:correlations")

    result = correlate_project(project)
    if result.ran:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect("logscan:correlations")


@login_required
def correlations(request):
    project = get_current_project(request)
    context = {"active_nav": "log-correlations", "project": project}
    if not project:
        return render(request, "logscan/correlations.html", context)

    groups = (LogPatternCorrelation.objects.filter(project=project)
              .prefetch_related("members__cluster__source")
              .order_by("-overlap_start"))
    context["groups"] = groups
    return render(request, "logscan/correlations.html", context)


def _build_incident_narrative_prompt(group):
    """BACKLOG A.10 (user-requested leadership-demo wow factor) — turns a
    correlation group's member patterns (each from a different LogSource,
    overlapping in time) into ONE incident story: what happened, in what
    order, across which services. example_line is already redact_pii()'d
    before it was ever saved (see LogPatternCluster's own docstring), so
    nothing extra to sanitize for this prompt."""
    members = sorted(group.members.all(), key=lambda m: m.cluster.first_line_at or group.overlap_start)
    lines = [
        "The patterns below were found in DIFFERENT log sources but overlapped in "
        "the same time window, suggesting one incident touching multiple systems. "
        "In 4-6 sentences, tell the detailed story of what likely happened, in time "
        "order - which service showed trouble first, quote the specific phrase from "
        "its example line that shows it, and explain what it likely triggered "
        "downstream and why. Name the actual source/service names throughout, not "
        "'the first service' - and say plainly if the patterns don't support a "
        "clear causal story.",
        "",
        f"Time window: {group.overlap_start:%H:%M:%S} - {group.overlap_end:%H:%M:%S} ({group.source_count} sources)",
        "",
        "Patterns, in time order:",
    ]
    for m in members:
        c = m.cluster
        when = c.first_line_at.strftime("%H:%M:%S") if c.first_line_at else "unknown time"
        lines.append(f"- [{when}] {c.source.name}: {c.name} ({c.recurring_count}x) — {c.example_line}")
    return "\n".join(lines)


@login_required
def generate_incident_narrative(request, pk):
    from core.llm_bridge import LLMBridgeUnavailable, ask
    project = get_current_project(request)
    group = get_object_or_404(LogPatternCorrelation, id=pk, project=project)
    if request.method != "POST":
        return redirect("logscan:correlations")
    try:
        group.ai_incident_narrative = ask(_build_incident_narrative_prompt(group))
        group.save(update_fields=["ai_incident_narrative"])
        messages.success(request, "Incident narrative generated via the Copilot HTTP Bridge.")
    except LLMBridgeUnavailable as exc:
        messages.error(request, f"Couldn't generate a narrative: {exc}")
    return redirect("logscan:correlations")


@login_required
def correlations_download(request):
    """CSV of every cross-source correlation group, flattened to one row per
    (group, member cluster) pair — a correlation is a group of clusters from
    different sources, so the group-level fields (overlap window, source
    count) repeat across that group's rows, same shape a spreadsheet user
    would want to pivot on."""
    project = get_current_project(request)
    if not project:
        return redirect("logscan:correlations")

    groups = (LogPatternCorrelation.objects.filter(project=project)
              .prefetch_related("members__cluster__source")
              .order_by("-overlap_start"))

    def iter_rows():
        for group in groups:
            for member in group.members.all():
                c = member.cluster
                yield (
                    group.overlap_start.strftime("%Y-%m-%d %H:%M:%S"), group.overlap_end.strftime("%Y-%m-%d %H:%M:%S"),
                    group.source_count, group.window_minutes, c.source.name, c.name, c.keywords, c.recurring_count,
                    c.first_line_at.strftime("%Y-%m-%d %H:%M:%S") if c.first_line_at else "",
                    c.last_line_at.strftime("%Y-%m-%d %H:%M:%S") if c.last_line_at else "",
                    c.example_line,
                )

    return csv_response(
        rows=iter_rows(),
        headers=["Overlap Start", "Overlap End", "Source Count", "Window (min)", "Source", "Pattern", "Keywords", "Recurring Count", "Pattern First Seen", "Pattern Last Seen", "Example (redacted)"],
        filename=f"cross_source_patterns_project{project.id}",
    )
