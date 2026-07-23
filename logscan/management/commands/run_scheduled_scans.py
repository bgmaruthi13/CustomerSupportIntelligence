from django.core.management.base import BaseCommand
from django.utils import timezone

from logscan.alerts import send_scan_digest
from logscan.models import LogScanJob, LogSource
from logscan.scanner import scan_directory, scan_stream
from logscan.sourcefiles import open_source_file, source_file_label


class Command(BaseCommand):
    help = (
        "Scans every active LogSource with trigger_mode='scheduled', resuming "
        "each from its last_scanned_offset. Not scheduled from inside Django - "
        "this command IS the scheduled unit of work; an external scheduler "
        "(Windows Task Scheduler, cron) is what actually runs it periodically. "
        "See deploy/windows/README.md for registering it. Runs synchronously, "
        "one source after another - it's already the background job (invoked "
        "by the scheduler, not by an HTTP request), so there's no need to "
        "sub-launch anything the way the on-demand trigger does."
    )

    def handle(self, *args, **options):
        sources = LogSource.objects.filter(is_active=True, trigger_mode="scheduled")
        if not sources:
            self.stdout.write("No active scheduled log sources configured.")
            return

        for source in sources:
            job = LogScanJob.objects.create(
                source=source, status="running", triggered_by="scheduled",
                start_offset=source.last_scanned_offset, started_at=timezone.now(),
            )
            try:
                if source.source_type == "directory":
                    scan_directory(source, job)
                else:
                    fileobj = open_source_file(source)
                    try:
                        final_offset = scan_stream(fileobj, job, scan_phones=source.scan_phone_numbers, file_path=source_file_label(source))
                    finally:
                        fileobj.close()
                    source.last_scanned_offset = final_offset
                    source.save(update_fields=["last_scanned_offset"])

                if job.findings_count:
                    send_scan_digest(job)

                self.stdout.write(self.style.SUCCESS(
                    f"{source.name}: {job.lines_scanned} lines, {job.findings_count} findings"
                ))
            except Exception as exc:  # noqa: BLE001 — one source failing must not stop the rest
                job.status = "failed"
                job.error_message = str(exc)
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "error_message", "finished_at"])
                self.stdout.write(self.style.ERROR(f"{source.name}: FAILED — {exc}"))
