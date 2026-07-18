import re

from django.db import models

from core.models import Project

# Nothing is enforced as mandatory in the mapping UI — the user chooses whichever
# columns they want for analysis. RECOMMENDED_FIELDS is informational only (a subtle
# hint that these matter most for clustering quality); anything left unmapped gets a
# sensible auto-generated fallback at commit time instead of blocking the import.
RECOMMENDED_FIELDS = ["external_id", "title", "created_at"]
OPTIONAL_FIELDS = ["description", "country", "application", "status", "priority", "offering", "requested_type", "assigned_to", "created_by", "queue"]
ALL_TARGET_FIELDS = RECOMMENDED_FIELDS + OPTIONAL_FIELDS

# Delimiters used to split a single "Offerings" cell into multiple values, e.g.
# "SunSystems Support, QA Vision" -> ["SunSystems Support", "QA Vision"]. A ticket
# can legitimately carry more than one offering, so this field is stored as the raw
# delimited string and exploded on read wherever it's counted/grouped/clustered.
MULTI_VALUE_DELIMITERS = re.compile(r"[,;|/]")

DATETIME_FORMAT_CHOICES = [
    ("auto", "Auto-detect"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%d/%m/%Y %H:%M", "DD/MM/YYYY HH:MM"),
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%m/%d/%Y %H:%M", "MM/DD/YYYY HH:MM"),
    ("%d-%b-%Y", "DD-Mon-YYYY (e.g. 05-Jul-2026)"),
    ("%Y-%m-%dT%H:%M:%S", "ISO 8601 (YYYY-MM-DDTHH:MM:SS)"),
]


class UploadBatch(models.Model):
    STATUS_CHOICES = [
        ("uploaded", "Uploaded — awaiting column mapping"),
        ("committed", "Committed"),
        ("failed", "Failed"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="upload_batches")
    file = models.FileField(upload_to="uploads/%Y/%m/")
    original_filename = models.CharField(max_length=255, blank=True)
    version = models.PositiveIntegerField(default=1, help_text="Sequential upload number within this project — resets when the project's data is reset")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="uploaded")
    column_mapping = models.JSONField(default=dict, blank=True)
    datetime_format = models.CharField(max_length=40, default="auto")
    detected_columns = models.JSONField(default=list, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    success_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    error_log = models.JSONField(default=list, blank=True, help_text="Rows that were skipped entirely (e.g. duplicate ID)")
    warning_log = models.JSONField(default=list, blank=True, help_text="Rows imported using an auto-generated fallback for an unmapped/unparseable field")
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.project.name})"

    @property
    def display_name(self):
        return f"Upload v{self.version}"


class Ticket(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tickets")
    upload_batch = models.ForeignKey(UploadBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets")

    external_id = models.CharField(max_length=100)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField()
    country = models.CharField(max_length=100, blank=True, default="Unspecified")
    application = models.CharField(max_length=150, blank=True, default="Unspecified")
    status = models.CharField(max_length=50, blank=True, default="Open")
    priority = models.CharField(max_length=20, blank=True, default="Medium")
    offering = models.CharField(max_length=255, blank=True, default="Unspecified", help_text="Raw value from the source column — may contain multiple offerings delimited by , ; | /")
    requested_type = models.CharField(max_length=100, blank=True, default="Unspecified")
    assigned_to = models.CharField(max_length=150, blank=True, default="Unassigned")
    created_by = models.CharField(max_length=150, blank=True, default="Unknown")
    queue = models.CharField(max_length=150, blank=True, default="Unspecified", help_text="Assignment group / team queue — distinct from application or offering")
    queue_source = models.CharField(
        max_length=20, blank=True, default="",
        choices=[("mapped", "From source column"), ("suggested", "AI-suggested, accepted")],
        help_text="How the current queue value was set — blank if queue is still Unspecified",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "created_at"]),
            models.Index(fields=["project", "country"]),
            models.Index(fields=["project", "application"]),
            models.Index(fields=["project", "offering"]),
        ]

    def __str__(self):
        return f"{self.external_id}: {self.title}"

    @property
    def text_blob(self):
        return f"{self.title}. {self.description}".strip()

    @property
    def offering_list(self):
        """Splits the raw offering value into its individual parts, e.g. "SunSystems
        Support, QA Vision" -> ["SunSystems Support", "QA Vision"]."""
        if not self.offering:
            return []
        parts = MULTI_VALUE_DELIMITERS.split(self.offering)
        return [p.strip() for p in parts if p.strip()]


class PIIFinding(models.Model):
    """A detected sensitive-data pattern in an uploaded file — regex/checksum
    matching (see tickets.pii_detection), not ML, and never the raw matched value.
    Scanned once at upload time across every column of the RAW file (not just
    mapped/imported ones), before Ticket rows exist yet — that's why `ticket` is
    nullable and `row_reference` is a standalone identifier (the external_id
    column's raw value if mapped, else the row's position in the file)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="pii_findings")
    upload_batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="pii_findings")
    ticket = models.ForeignKey(Ticket, null=True, blank=True, on_delete=models.SET_NULL, related_name="pii_findings")
    source_column = models.CharField(max_length=150)
    row_reference = models.CharField(max_length=100, blank=True)
    pii_type = models.CharField(max_length=20, choices=[
        ("email", "Email"), ("phone", "Phone Number"), ("card", "Credit/Debit Card"),
        ("iban", "IBAN"), ("account_number", "Bank Account Number"), ("address", "Address"),
    ])
    confidence = models.CharField(max_length=10, choices=[("high", "High"), ("medium", "Medium"), ("low", "Low")])
    context = models.CharField(
        max_length=20, default="free_text",
        choices=[
            ("identity_field", "Expected Identity Field"),
            ("unmapped_column", "Unmapped Column"),
            ("free_text", "Free-Text Field"),
        ],
        help_text="Whether this finding is in a column expected to hold an identity "
                   "(created_by/assigned_to — not a surprise), an unmapped column "
                   "(data nobody chose to import), or an ordinary mapped free-text "
                   "field. Used to surface unmapped_column/free_text findings first, "
                   "since a name/email in an identity column isn't a leak.",
    )
    masked_preview = models.CharField(max_length=100, help_text="e.g. j***@***.com — never the raw value")
    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["upload_batch"]), models.Index(fields=["project"])]

    def __str__(self):
        return f"{self.pii_type} in {self.source_column} ({self.confidence})"
