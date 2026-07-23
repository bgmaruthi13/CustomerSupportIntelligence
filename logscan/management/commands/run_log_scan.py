from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from logscan.models import LogScanJob
from logscan.scanner import scan_directory, scan_stream
from logscan.sourcefiles import open_source_file, source_file_label


class Command(BaseCommand):
    help = (
        "Runs one LogScanJob to completion, streaming the job's LogSource file "
        "and never loading it into memory - this is what makes 100GB+ files "
        "scannable. Meant to be launched detached (see logscan.views.scan_now, "
        "which is the on-demand trigger) or invoked by an external scheduler/"
        "service (run_scheduled_scans, tail_log_sources) - not run inline "
        "during an HTTP request, since a scan can take a long time."
    )

    def add_arguments(self, parser):
        parser.add_argument("--job-id", type=int, required=True)

    def handle(self, *args, **options):
        job_id = options["job_id"]
        try:
            job = LogScanJob.objects.select_related("source").get(id=job_id)
        except LogScanJob.DoesNotExist:
            raise CommandError(f"LogScanJob {job_id} does not exist.")

        source = job.source
        job.status = "running"
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at"])

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

            self.stdout.write(self.style.SUCCESS(
                f"Job {job.id}: scanned {job.lines_scanned} lines, {job.findings_count} findings."
            ))

            if job.findings_count:
                from logscan.alerts import send_scan_digest
                send_scan_digest(job)

        except Exception as exc:  # noqa: BLE001 — any failure must land the job in a terminal state, not "running" forever
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error_message", "finished_at"])
            raise CommandError(f"Job {job.id} failed: {exc}")
