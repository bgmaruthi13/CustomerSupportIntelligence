from django.conf import settings
from django.db import models

from core.models import Project
from tickets.pii_detection import CONFIDENCE_CHOICES, PII_TYPES

SOURCE_TYPE_CHOICES = [
    ("path", "Filesystem / network path (single file)"),
    ("upload", "Uploaded file"),
    ("directory", "Directory / NAS share (many files)"),
]

TRIGGER_MODE_CHOICES = [
    ("on_demand", "On-demand"),
    ("scheduled", "Scheduled"),
    ("continuous", "Continuous (tailing)"),
]

JOB_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("running", "Running"),
    ("completed", "Completed"),
    ("failed", "Failed"),
]

JOB_TRIGGERED_BY_CHOICES = [
    ("manual", "Manual (on-demand)"),
    ("scheduled", "Scheduled"),
    ("continuous", "Continuous (tailing)"),
]


class LogSource(models.Model):
    """A configured log file/location to scan for sensitive data — deliberately
    separate from tickets.UploadBatch (which is a one-shot, whole-file-in-memory
    ticket import). A log source can be re-scanned many times (scheduled,
    continuous), and at 100GB+ can never be loaded into memory the way a ticket
    export is — see logscan.scanner for the streaming read path.

    scan_phone_numbers defaults off (unlike tickets.pii_detection, which always
    checks phone numbers): phonenumbers.PhoneNumberMatcher does real parsing
    and is the most expensive check in detect_pii() by far, and most log lines
    contain digits (timestamps, ports, IDs) that would trigger it on every
    line. Email/card/IBAN are cheap regex passes and stay on unconditionally —
    they're also the higher-value signal for a log-leak scenario. An operator
    who wants phone coverage can turn it back on knowingly, per source."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="log_sources")
    name = models.CharField(max_length=200, help_text="A label for this source, e.g. 'Prod API gateway logs'")

    source_type = models.CharField(max_length=10, choices=SOURCE_TYPE_CHOICES, default="path")
    path = models.CharField(max_length=1000, blank=True, help_text="Absolute filesystem or network path — a single file for 'path', a folder for 'directory' (e.g. a mapped NAS share like \\\\fileserver\\logs)")
    uploaded_file = models.FileField(upload_to="logscan/%Y/%m/", null=True, blank=True, help_text="Required when source_type is 'upload'")

    # 'directory' source_type only — which files under `path` count as logs to
    # scan, and how deep to look. A NAS log share can hold non-log files
    # (archives, configs, other apps' data) sitting alongside what you
    # actually want scanned; scanning indiscriminately would waste time on
    # (and risk erroring on) files that were never meant to be read this way.
    file_pattern = models.CharField(max_length=200, default="*.log", blank=True, help_text="Glob pattern for which files count as logs, e.g. '*.log' or '*.txt'. Directory sources only.")
    recursive = models.BooleanField(default=True, help_text="Include subfolders. Directory sources only.")

    trigger_mode = models.CharField(max_length=12, choices=TRIGGER_MODE_CHOICES, default="on_demand")
    is_active = models.BooleanField(default=True, help_text="Scheduled/continuous scans skip inactive sources entirely")
    scan_phone_numbers = models.BooleanField(default=False, help_text="Off by default — see model docstring for why this is the one detector worth gating at log scale")

    alert_emails = models.CharField(max_length=500, blank=True, help_text="Comma-separated. One digest email per scan that finds anything, never one email per finding.")

    # Byte offset this source has been scanned up to — the resume point for
    # continuous tailing (logscan.tail_log_sources) and for a scheduled re-scan
    # that only wants to cover what's new since last time, not the whole file
    # again. Set at the end of every successful scan_stream() call regardless
    # of trigger mode, so any mode can pick up where another left off.
    # Meaningless for source_type='directory' — a directory has no single
    # offset, since it's N files each with their own; see LogSourceFile for
    # the per-file equivalent of this field.
    last_scanned_offset = models.BigIntegerField(default=0)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def display_location(self):
        if self.source_type == "upload":
            return self.uploaded_file.name if self.uploaded_file else "(no file)"
        return self.path


class LogScanJob(models.Model):
    """One run of scanning a LogSource — whether triggered by a person clicking
    'Scan now', an externally-scheduled `run_scheduled_scans` invocation, or one
    pass of the `tail_log_sources` continuous loop. Progress fields are updated
    incrementally by the scanner (see logscan.scanner.scan_stream) so a
    multi-hour job on a 100GB file has a live status to poll, not just a
    final result."""

    source = models.ForeignKey(LogSource, on_delete=models.CASCADE, related_name="jobs")
    status = models.CharField(max_length=10, choices=JOB_STATUS_CHOICES, default="pending")
    triggered_by = models.CharField(max_length=10, choices=JOB_TRIGGERED_BY_CHOICES, default="manual")

    start_offset = models.BigIntegerField(default=0, help_text="Byte offset this job started scanning from")
    bytes_total = models.BigIntegerField(null=True, blank=True, help_text="Null when unknown ahead of time (e.g. a growing log)")
    bytes_scanned = models.BigIntegerField(default=0)
    lines_scanned = models.BigIntegerField(default=0)
    findings_count = models.PositiveIntegerField(default=0)

    error_message = models.TextField(blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source.name} [{self.status}] ({self.findings_count} findings)"

    @property
    def progress_pct(self):
        if not self.bytes_total:
            return None
        return round(min(100, self.bytes_scanned / self.bytes_total * 100))


class LogSourceFile(models.Model):
    """One file discovered under a source_type='directory' LogSource — the
    per-file equivalent of LogSource.last_scanned_offset, since a directory
    scan has to track N independent resume points instead of one. Identified
    by relative_path (relative to the source's own `path`), not an absolute
    path, so the row stays valid even if the share gets remounted under a
    different drive letter/UNC prefix."""

    source = models.ForeignKey(LogSource, on_delete=models.CASCADE, related_name="files")
    relative_path = models.CharField(max_length=1000)
    last_scanned_offset = models.BigIntegerField(default=0)
    last_seen_at = models.DateTimeField(auto_now=True, help_text="Updated every time this file is seen during directory enumeration — a file that stops being seen has likely been deleted/rotated away.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("source", "relative_path")]
        ordering = ["relative_path"]

    def __str__(self):
        return f"{self.source.name}/{self.relative_path}"


class LogPIIFinding(models.Model):
    """A detected sensitive-data pattern in a scanned log line — same masking
    philosophy as tickets.models.PIIFinding: the raw matched value is never
    stored, only detect_pii()'s masked_preview. line_number/byte_offset locate
    the match within the source file without needing to keep the raw line
    text around.

    file_path is always populated, even for single-file sources (path/upload)
    — it's just that file's own name/path in that case — rather than being
    conditionally blank, so the findings report never needs a special case to
    tell "which file" apart from "which source"."""

    job = models.ForeignKey(LogScanJob, on_delete=models.CASCADE, related_name="findings")
    source = models.ForeignKey(LogSource, on_delete=models.CASCADE, related_name="findings")

    file_path = models.CharField(max_length=1000, blank=True)
    line_number = models.PositiveBigIntegerField()
    byte_offset = models.BigIntegerField()

    pii_type = models.CharField(max_length=20, choices=PII_TYPES)
    confidence = models.CharField(max_length=10, choices=CONFIDENCE_CHOICES)
    masked_preview = models.CharField(max_length=100)

    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-detected_at"]
        indexes = [models.Index(fields=["source"]), models.Index(fields=["job"])]

    def __str__(self):
        return f"{self.pii_type} in {self.file_path}:{self.line_number}"
