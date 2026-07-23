import os
import signal
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from logscan.alerts import send_scan_digest
from logscan.models import LogScanJob, LogSource, LogSourceFile
from logscan.scanner import iter_directory_files, scan_directory, scan_stream
from logscan.sourcefiles import open_source_file, source_file_label

POLL_SECONDS = 5


class Command(BaseCommand):
    help = (
        "Long-running loop: watches every active LogSource with "
        "trigger_mode='continuous' and scans new bytes as they're appended - "
        "real-time, not a periodic batch. Meant to run as its own always-on "
        "Windows Service (see deploy/windows/install-log-watcher-service.ps1), "
        "separate from the main app service, so restarting/updating one "
        "doesn't affect the other. source_type 'path' and 'directory' both "
        "make sense here (a directory of growing logs is if anything the "
        "most natural fit for tailing); 'upload' sources are skipped with a "
        "warning - an uploaded file is a one-shot static snapshot with "
        "nothing to tail."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single poll pass instead of looping forever - used by tests.")
        parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)

    def handle(self, *args, **options):
        self._stop = False

        def _handle_signal(signum, frame):
            self.stdout.write("tail_log_sources: shutdown signal received, stopping after this pass.")
            self._stop = True

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        warned_upload_sources = set()

        while not self._stop:
            sources = LogSource.objects.filter(is_active=True, trigger_mode="continuous")
            for source in sources:
                if source.source_type == "upload":
                    if source.id not in warned_upload_sources:
                        self.stdout.write(self.style.WARNING(
                            f"{source.name}: continuous tailing needs source_type='path' or 'directory' (an uploaded file can't grow) - skipping."
                        ))
                        warned_upload_sources.add(source.id)
                    continue
                if source.source_type == "directory":
                    self._tail_directory(source)
                else:
                    self._tail_one(source)

            if options["once"]:
                break
            time.sleep(options["poll_seconds"])

    def _tail_one(self, source):
        if not source.path or not os.path.exists(source.path):
            return
        if os.path.isdir(source.path):
            # Caught here, not just left to open_source_file() below: a
            # directory's reported size doesn't grow the way a log file's
            # does, so without this check a misconfigured directory source
            # would silently poll forever without ever reaching the open()
            # call that would otherwise raise a clear error - no error, but
            # also never actually scanning anything.
            self.stdout.write(self.style.ERROR(
                f"{source.name}: path '{source.path}' is a directory, not a file - use source_type='directory' for this source instead."
            ))
            return

        current_size = os.path.getsize(source.path)

        # Log rotation / truncation: if the file is now smaller than where we
        # last left off, the old offset points past the new EOF - treat it as
        # a fresh file rather than erroring on a seek past the end (or, worse,
        # silently never scanning again because current_size never catches up
        # to a stale offset).
        if current_size < source.last_scanned_offset:
            self.stdout.write(self.style.WARNING(f"{source.name}: file shrank (rotated?) - resetting offset to 0."))
            source.last_scanned_offset = 0
            source.save(update_fields=["last_scanned_offset"])

        if current_size <= source.last_scanned_offset:
            return  # nothing new to scan

        job = LogScanJob.objects.create(
            source=source, status="running", triggered_by="continuous",
            start_offset=source.last_scanned_offset, started_at=timezone.now(),
        )
        try:
            with open_source_file(source) as fileobj:
                final_offset = scan_stream(fileobj, job, scan_phones=source.scan_phone_numbers, file_path=source_file_label(source))
            source.last_scanned_offset = final_offset
            source.save(update_fields=["last_scanned_offset"])
            if job.findings_count:
                send_scan_digest(job)
            self.stdout.write(f"{source.name}: +{job.lines_scanned} lines, {job.findings_count} new findings")
        except Exception as exc:  # noqa: BLE001 — one source failing must not kill the whole watch loop
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error_message", "finished_at"])
            self.stdout.write(self.style.ERROR(f"{source.name}: FAILED — {exc}"))

    def _tail_directory(self, source):
        if not source.path or not os.path.isdir(source.path):
            return

        # Cheap pre-check (list + stat, no reads) before deciding whether this
        # poll cycle is worth a LogScanJob row at all - a directory being
        # watched every 5s that has nothing new most cycles shouldn't fill
        # the job history with hundreds of empty "0 findings" jobs the way
        # scan_directory() would produce if called unconditionally every pass
        # (see scan_directory's own docstring: it always creates exactly one
        # job per call, by design, for the on-demand/scheduled callers where
        # that's the desired audit trail).
        has_new = False
        for relative_path, absolute_path in iter_directory_files(source):
            file_row, _created = LogSourceFile.objects.get_or_create(source=source, relative_path=relative_path)
            try:
                current_size = os.path.getsize(absolute_path)
            except OSError:
                continue
            if current_size != file_row.last_scanned_offset:
                has_new = True
                break
        if not has_new:
            return

        job = LogScanJob.objects.create(
            source=source, status="running", triggered_by="continuous", started_at=timezone.now(),
        )
        try:
            scan_directory(source, job)
            if job.findings_count:
                send_scan_digest(job)
            self.stdout.write(f"{source.name}: +{job.lines_scanned} lines this pass, {job.findings_count} new findings")
        except Exception as exc:  # noqa: BLE001 — one source failing must not kill the whole watch loop
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error_message", "finished_at"])
            self.stdout.write(self.style.ERROR(f"{source.name}: FAILED — {exc}"))
