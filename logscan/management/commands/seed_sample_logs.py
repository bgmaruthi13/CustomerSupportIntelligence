from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Project
from logscan.correlation import correlate_project
from logscan.models import LogScanJob, LogSource
from logscan.pattern_analysis import analyze_patterns
from logscan.scanner import scan_directory
from logscan.sourcefiles import open_source_file, source_file_label
from logscan.scanner import scan_stream

SAMPLE_NAME_PREFIX = "Sample: "


class Command(BaseCommand):
    help = (
        "Seeds demo Log Sources (single file, scheduled, and directory) pointing at "
        "the fixture logs under sample_data/logs/, then actually runs the real scan, "
        "Find Patterns, and cross-source correlation pipelines against them - not "
        "fake DB rows, genuine LogPIIFinding/LogPatternCluster/LogPatternCorrelation "
        "output from the real code path, so the Log Monitoring pages have something "
        "to show. Safe to re-run: deletes any previous 'Sample: ...' sources for the "
        "target project first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--project", type=str, default=None, help="Project name to seed into. Defaults to the only project, if there's exactly one.")

    def handle(self, *args, **options):
        project = self._resolve_project(options["project"])
        base = settings.BASE_DIR / "sample_data" / "logs"
        if not base.exists():
            raise CommandError(f"Expected sample log files under {base} - did sample_data/logs/ get removed?")

        LogSource.objects.filter(project=project, name__startswith=SAMPLE_NAME_PREFIX).delete()

        api_gateway = LogSource.objects.create(
            project=project, name=f"{SAMPLE_NAME_PREFIX}API Gateway Logs",
            source_type="path", path=str(base / "api_gateway.log"), trigger_mode="on_demand",
        )
        payment_service = LogSource.objects.create(
            project=project, name=f"{SAMPLE_NAME_PREFIX}Payment Service Logs",
            source_type="path", path=str(base / "payment_service.log"), trigger_mode="scheduled",
        )
        app_servers = LogSource.objects.create(
            project=project, name=f"{SAMPLE_NAME_PREFIX}App Servers (folder)",
            source_type="directory", path=str(base / "app_servers"), trigger_mode="on_demand",
            file_pattern="*.log", recursive=True,
            # web01.log's only planted PII is a phone number - scan_phone_numbers
            # defaults off (see LogSource docstring), so leaving this False would
            # make the directory-source demo show zero findings.
            scan_phone_numbers=True,
        )

        # Two more sources, deliberately with overlapping ISO timestamps
        # (2026-07-23 09:00-09:10) across DIFFERENT tools' logs, so
        # correlate_project() below has a real cross-source incident to
        # find: checkout-service timing out on inventory lookups while
        # inventory-service is independently logging DB pool exhaustion —
        # the same underlying incident, visible in two unrelated logs.
        checkout_service = LogSource.objects.create(
            project=project, name=f"{SAMPLE_NAME_PREFIX}Checkout Service Logs",
            source_type="path", path=str(base / "checkout_service.log"), trigger_mode="on_demand",
        )
        inventory_service = LogSource.objects.create(
            project=project, name=f"{SAMPLE_NAME_PREFIX}Inventory Service Logs",
            source_type="path", path=str(base / "inventory_service.log"), trigger_mode="on_demand",
        )

        for source in (api_gateway, payment_service, checkout_service, inventory_service):
            self._run_scan(source)
            result = analyze_patterns(source)
            self.stdout.write(f"  Find Patterns on {source.name}: {result.message}")

        self._run_directory_scan(app_servers)

        correlation_result = correlate_project(project)
        self.stdout.write(f"  Cross-source correlation: {correlation_result.message}")

        self.stdout.write(self.style.SUCCESS(
            f"Seeded 5 sample log sources into '{project.name}' — see Log Sources, Log PII Alerts, each source's Find Patterns page, and Cross-Source Patterns."
        ))

    def _resolve_project(self, name):
        if name:
            try:
                return Project.objects.get(name=name)
            except Project.DoesNotExist:
                raise CommandError(f"No project named '{name}'.")
        projects = list(Project.objects.all())
        if len(projects) == 1:
            return projects[0]
        raise CommandError(
            f"Found {len(projects)} projects - pass --project '<name>' to pick one "
            f"({', '.join(p.name for p in projects)})." if projects else
            "No projects exist yet - create one first."
        )

    def _run_scan(self, source):
        job = LogScanJob.objects.create(source=source, status="running", triggered_by="manual", started_at=timezone.now())
        fileobj = open_source_file(source)
        try:
            final_offset = scan_stream(fileobj, job, scan_phones=source.scan_phone_numbers, file_path=source_file_label(source))
        finally:
            fileobj.close()
        source.last_scanned_offset = final_offset
        source.save(update_fields=["last_scanned_offset"])
        self.stdout.write(f"  Scanned {source.name}: {job.lines_scanned} lines, {job.findings_count} findings")

    def _run_directory_scan(self, source):
        job = LogScanJob.objects.create(source=source, status="running", triggered_by="manual", started_at=timezone.now())
        scan_directory(source, job)
        self.stdout.write(f"  Scanned {source.name}: {job.lines_scanned} lines, {job.findings_count} findings across {source.files.count()} files")
