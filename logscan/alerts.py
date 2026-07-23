"""Email digest alerting — one email per completed scan job that found
anything, never one per finding (a 100GB file with a real leak could produce
thousands of findings; a thousand emails would bury the one thing that
matters). SMTP settings (added in correlate/settings.py) are env-var-driven
like every other setting in this app; if they're not configured, this
degrades to a logged no-op rather than failing the scan job itself — a
missing alert channel shouldn't make the scan job report as failed when the
scan itself succeeded.
"""

import logging

from django.core.mail import send_mail

from tickets.pii_detection import PII_TYPES

logger = logging.getLogger("django")


def send_scan_digest(job):
    source = job.source
    recipients = [e.strip() for e in source.alert_emails.split(",") if e.strip()]
    if not recipients:
        return

    pii_type_labels = dict(PII_TYPES)
    by_type = {}
    for f in job.findings.values_list("pii_type", flat=True):
        by_type[f] = by_type.get(f, 0) + 1

    lines = [
        f"Log source: {source.name} ({source.display_location})",
        f"Scan finished: {job.finished_at:%Y-%m-%d %H:%M} UTC",
        f"Lines scanned: {job.lines_scanned:,}",
        f"Findings: {job.findings_count:,}",
        "",
    ]
    lines += [f"  {pii_type_labels.get(t, t)}: {c}" for t, c in sorted(by_type.items(), key=lambda kv: -kv[1])]
    lines += ["", "Masked previews only — review the full report in Correlate AI's Log PII Alerts page."]
    body = "\n".join(lines)

    try:
        send_mail(
            subject=f"[Correlate AI] {job.findings_count} sensitive-data finding(s) in '{source.name}'",
            message=body,
            from_email=None,  # falls back to DEFAULT_FROM_EMAIL
            recipient_list=recipients,
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 — email delivery failing must never fail the scan job it's reporting on
        logger.warning("logscan: failed to send alert digest for job %s: %s", job.id, exc)
