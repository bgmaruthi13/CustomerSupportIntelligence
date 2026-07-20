from django.conf import settings
from django.db import models

from core.models import Project
from tickets.models import Ticket

ENGINE_CHOICES = [
    ("traditional_ml", "Traditional ML"),
    ("generative_ai", "Generative AI"),
]

GRANULARITY_CHOICES = [
    ("broad", "Broad"),
    ("balanced", "Balanced"),
    ("fine", "Fine-grained"),
]

TREND_CHOICES = [
    ("rising", "Rising"),
    ("stable", "Stable"),
    ("falling", "Falling"),
]


class Cluster(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="clusters")
    engine = models.CharField(max_length=20, choices=ENGINE_CHOICES)
    name = models.CharField(max_length=255)
    keywords = models.CharField(max_length=500, blank=True)
    confidence = models.FloatField(default=0.0)
    recurring_count = models.PositiveIntegerField(default=0)
    trend = models.CharField(max_length=10, choices=TREND_CHOICES, default="stable")
    trend_reasoning = models.CharField(max_length=500, blank=True, help_text="Plain-English basis for the trend classification, e.g. counts and date windows compared")
    is_problem_candidate = models.BooleanField(default=False)
    is_noise = models.BooleanField(default=False, help_text="True for the catch-all 'unclustered' bucket")
    top_country = models.CharField(max_length=100, blank=True)
    top_application = models.CharField(max_length=150, blank=True)
    top_offering = models.CharField(max_length=255, blank=True)
    source_description = models.CharField(max_length=255, blank=True, help_text="Human-readable description of the dataset scope this run used, e.g. 'All 20,113 tickets' or 'sample_2026.xlsx (2,000 tickets)'")
    granularity = models.CharField(max_length=10, choices=GRANULARITY_CHOICES, default="balanced", help_text="Which granularity preset produced this run")
    regional_trends = models.JSONField(default=list, blank=True, help_text="Per-country trend breakdown, top 5 countries by ticket count: [{country, trend, reasoning, count}, ...]")
    regional_divergence = models.BooleanField(default=False, help_text="True when a region is rising while the cluster's overall trend isn't — a problem that looks fine in aggregate")
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='sub_clusters', help_text="Set when this cluster is a drill-down sub-cluster of another cluster, rather than a top-level pipeline result")
    ai_summary = models.CharField(
        max_length=500, blank=True,
        help_text="One-line plain-English problem summary — pasted back from an external AI "
                   "chat (Copilot/Claude/etc.) via the same copy/paste bridge as Resolution. "
                   "Supplements the mechanical TF-IDF `name`/`keywords` with a sentence a "
                   "support manager would actually say out loud.",
    )
    ai_trend_explanation = models.CharField(
        max_length=500, blank=True,
        help_text="AI-drafted hypothesis for *why* this cluster's trend moved the way it did "
                   "(e.g. a shared vendor, a recent change) — pasted back via the same bridge. "
                   "Supplements trend_reasoning, which only states the count/date-window basis "
                   "for the label, not a cause.",
    )
    resolution_notes = models.TextField(blank=True, help_text="Either analyst-authored or pasted back from an external AI chat (e.g. Copilot) — see resolution_source")
    resolution_source = models.CharField(max_length=20, blank=True, choices=[("manual", "Analyst-authored"), ("copilot_assisted", "AI-suggested via Copilot")])
    resolution_added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolution_added_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recurring_count"]

    def __str__(self):
        return f"[{self.engine}] {self.name} ({self.recurring_count})"

    @property
    def engine_label(self):
        return dict(ENGINE_CHOICES).get(self.engine, self.engine)

    @property
    def color(self):
        return "#0c7973" if self.engine == "traditional_ml" else "#6c4fd8"


class ClusterMember(models.Model):
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="members")
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="cluster_memberships")
    similarity = models.FloatField(default=0.0)
    x = models.FloatField(null=True, blank=True)
    y = models.FloatField(null=True, blank=True)
    z = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = [("cluster", "ticket")]

    def __str__(self):
        return f"{self.ticket.external_id} -> {self.cluster.name}"


class TicketEmbedding(models.Model):
    """Persisted sentence-embedding vector for a ticket — the retrieval index behind
    Ask Correlate. Populated as a side effect of the Generative AI pipeline (which
    already embeds every ticket for clustering) or by the standalone search-index
    build action for projects that haven't run it. `project` is denormalized so a
    project's full index loads with one indexed query instead of a join through
    `ticket`."""

    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="embedding_record")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="ticket_embeddings")
    vector = models.BinaryField(help_text="float32 numpy array, packed via .tobytes()")
    dims = models.PositiveSmallIntegerField()
    model_path = models.CharField(max_length=300, help_text="Which embedding model produced this vector — stale rows are detected by comparing this to the currently configured model")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["project"])]

    def __str__(self):
        return f"embedding({self.ticket.external_id})"


class GlobalCluster(models.Model):
    """A cluster produced by a cross-project run (see clustering.pipelines.
    run_global_clustering) — deliberately NOT a Cluster with project=None. Cluster and
    everything that queries it assumes exactly one project throughout the app (cluster
    lists, dashboard counts, KPI calcs, the drill-down `parent` FK); reusing it here
    would mean auditing every one of those call sites for a null-project case that only
    this one admin-facing report ever produces. A parallel model keeps the well-tested
    single-project path completely untouched.

    Wiped and rebuilt on every run, scoped by engine only (not by any single project,
    since there isn't one) — same "delete then recreate" convention _save_clusters
    already uses."""

    engine = models.CharField(max_length=20, choices=ENGINE_CHOICES)
    name = models.CharField(max_length=255)
    keywords = models.CharField(max_length=500, blank=True)
    confidence = models.FloatField(default=0.0)
    recurring_count = models.PositiveIntegerField(default=0)
    trend = models.CharField(max_length=10, choices=TREND_CHOICES, default="stable")
    trend_reasoning = models.CharField(max_length=500, blank=True)
    is_noise = models.BooleanField(default=False)
    top_country = models.CharField(max_length=100, blank=True)
    top_application = models.CharField(max_length=150, blank=True)
    top_offering = models.CharField(max_length=255, blank=True)
    granularity = models.CharField(max_length=10, choices=GRANULARITY_CHOICES, default="balanced")
    source_description = models.CharField(max_length=255, blank=True, help_text="Which projects/tickets this run covered")
    project_count = models.PositiveSmallIntegerField(default=0, help_text="Distinct projects among this cluster's members — 2 or more means a cross-project intersection")
    is_significant_intersection = models.BooleanField(
        default=False,
        help_text="True only when the non-majority project(s) contribute enough tickets "
                   "(>=3 and >=10% of the cluster) to credibly be a shared incident, not just "
                   "a coincidental stray ticket. project_count alone (>=2) is too weak a bar for "
                   "the 'Cross-Project Intersection' badge — a 71/3 split isn't the same claim "
                   "as a 40/34 split, even though both have project_count=2.",
    )
    run_at = models.DateTimeField(auto_now_add=True)
    run_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-project_count", "-recurring_count"]

    def __str__(self):
        return f"[global:{self.engine}] {self.name} ({self.project_count} projects)"

    @property
    def color(self):
        return "#0c7973" if self.engine == "traditional_ml" else "#6c4fd8"


class GlobalClusterMember(models.Model):
    cluster = models.ForeignKey(GlobalCluster, on_delete=models.CASCADE, related_name="members")
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="global_cluster_memberships")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="global_cluster_memberships", help_text="Denormalized from ticket.project — this is the whole point of the model, grouping by it is the main query")
    similarity = models.FloatField(default=0.0)
    x = models.FloatField(null=True, blank=True)
    y = models.FloatField(null=True, blank=True)
    z = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = [("cluster", "ticket")]

    def __str__(self):
        return f"{self.ticket.external_id} -> {self.cluster.name}"


class ClusteringSettings(models.Model):
    """Per-project, per-engine configuration for what text feeds clustering and how
    it's preprocessed before vectorizing/embedding. Traditional ML and Generative AI
    get independently configurable rows — heavy preprocessing (lowercasing, stopword
    removal, stripping punctuation) helps TF-IDF but actively degrades sentence-
    embedding quality, so the two engines' defaults deliberately differ (see
    clustering.settings_utils.default_settings_for). No row existing yet for a given
    (project, engine) means "use the defaults" — there's no migration/backfill step,
    get_or_default() in clustering.settings_utils returns an unsaved default instance
    instead."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="clustering_settings")
    engine = models.CharField(max_length=20, choices=ENGINE_CHOICES)

    # --- Source column selection ---
    source_fields = models.JSONField(default=list, help_text='Ordered Ticket field names to concatenate into the clustering text, e.g. ["title", "description"]')

    # --- Cleanup toggles (generally safe for both engines — remove things that were never meaningful natural language) ---
    normalize_whitespace = models.BooleanField(default=True, help_text="Collapse tabs/newlines/runs of spaces to one space")
    strip_html = models.BooleanField(default=True, help_text="Strip tags/entities from rich-text exports")
    remove_urls = models.BooleanField(default=True)
    remove_emails = models.BooleanField(default=True)
    strip_boilerplate = models.BooleanField(default=False)
    boilerplate_patterns = models.JSONField(default=list, help_text='Phrases (or, if is_regex, regexes) to strip verbatim, e.g. signature blocks or "This is an automated message..." footers. Each entry: {"pattern": str, "is_regex": bool}')
    normalize_unicode = models.BooleanField(default=False, help_text='Accent/diacritic stripping, e.g. "café" -> "cafe"')
    strip_ticket_id_patterns = models.BooleanField(default=False)
    ticket_id_patterns = models.JSONField(default=list, help_text='Same shape as boilerplate_patterns, e.g. {"pattern": "INC\\\\d{7}", "is_regex": true}')

    # --- Aggressive text-shape toggles ---
    lowercase = models.BooleanField(default=True)
    keep_only_text = models.BooleanField(default=False, help_text="Strip everything but letters/whitespace — implies remove_special_characters and remove_numbers")
    remove_special_characters = models.BooleanField(default=True)
    remove_numbers = models.BooleanField(default=True)

    # --- Token-level cleanup ---
    remove_stopwords = models.BooleanField(default=True)
    stopwords_add = models.JSONField(default=list, help_text="Extra words appended to the base English stopword list")
    stopwords_exclude = models.JSONField(default=list, help_text="Words removed from the base list even if present — e.g. keep a normally-generic word that's meaningful in this project's data")
    min_token_length = models.PositiveSmallIntegerField(default=1, help_text="1 = off; e.g. 3 drops tokens under 3 characters")
    enable_stemming = models.BooleanField(default=False, help_text='Collapses inflected forms to a common stem, e.g. "failed"/"failing"/"fails" -> one token')

    # --- Vectorizer-level (Traditional ML only — corpus statistics, not per-ticket text preprocessing) ---
    min_document_frequency = models.PositiveSmallIntegerField(default=2, help_text="TfidfVectorizer min_df — drop terms appearing in fewer than this many tickets")
    max_document_frequency = models.FloatField(default=0.95, help_text="TfidfVectorizer max_df — drop terms appearing in more than this fraction of tickets")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("project", "engine")]

    def __str__(self):
        return f"ClusteringSettings({self.project_id}, {self.engine})"


class DuplicateCandidate(models.Model):
    """A proactively-detected likely-duplicate ticket pair — distinct from Find
    Similar Tickets (user picks one ticket, explores ad hoc): this is a standing
    data-hygiene worklist from a project-wide scan. High embedding similarity alone
    only means "same topic" (that's what Cluster is for) — a duplicate claim also
    needs close timing and a matching reporter/app to be credible, both checked at
    scan time in clustering.pipelines.scan_for_duplicates."""

    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("confirmed", "Confirmed Duplicate"),
        ("dismissed", "Not a Duplicate"),
    ]

    ticket_a = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="duplicate_candidates_a")
    ticket_b = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="duplicate_candidates_b")
    similarity = models.FloatField(help_text="Cosine similarity, 0-100")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("ticket_a", "ticket_b")]
        ordering = ["-similarity"]

    def __str__(self):
        return f"{self.ticket_a.external_id} ~ {self.ticket_b.external_id} ({self.similarity:.0f}%)"
