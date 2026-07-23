import subprocess
import sys

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.utils import apply_sort, get_current_project
from logscan.models import LogPatternCluster, LogPIIFinding, LogScanJob, LogSource, SOURCE_TYPE_CHOICES, TRIGGER_MODE_CHOICES
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
