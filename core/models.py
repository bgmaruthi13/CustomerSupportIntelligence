import random

from django.conf import settings
from django.db import models

PALETTE = ["#2563eb", "#d97706", "#7c3aed", "#0e8c86", "#dc2626", "#16a34a", "#db2777"]


class SiteSettings(models.Model):
    """Singleton (always pk=1) holding app-wide branding and pipeline configuration
    that admins can change without touching code or templates."""

    site_name = models.CharField(max_length=100, default="Correlate AI", help_text="Shown in the sidebar, browser tab title, and login page — no logo image, text only.")
    embedding_model_path = models.CharField(
        max_length=300, default="embedding_model", blank=True,
        help_text="Local folder containing a sentence-transformers model, used by Generative AI, Smart Search, Find Similar, Smart Assign, and Possible Duplicates. Either a path relative to the app's root folder (e.g. 'embedding_model') or an absolute local path — never a Hugging Face Hub model id; this app never reaches out to Hugging Face. If the folder doesn't exist, those features show a clear error instead of failing silently. Also editable from the Clustering Settings page.",
    )

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return "Site Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # singleton — never actually delete

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Project(models.Model):
    """A tenant workspace. All tickets/clusters are scoped to a Project."""

    name = models.CharField(max_length=200)
    domain = models.CharField(max_length=200, blank=True, help_text="System/domain this project covers, e.g. 'Order Management · Payments'")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="projects")
    color = models.CharField(max_length=7, default="#2563eb")
    is_sample = models.BooleanField(default=False, help_text="Seeded demo data, not a real upload")
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Confidence Score weighting — project-wide (not per-engine), since a cluster's
    # confidence % is meant to mean the same thing regardless of which engine produced
    # it. Defaults reproduce today's original fixed formula in clustering/scoring.py
    # exactly; normalized at computation time (see compute_confidence), not validated
    # at save time, so any three numbers a user enters always sum to 100% behind the
    # scenes without a "must add up to 100" error.
    confidence_weight_size = models.FloatField(default=0.40, help_text="How much cluster size (relative to this run's largest cluster) counts toward confidence")
    confidence_weight_density = models.FloatField(default=0.35, help_text="How much membership density (how tightly HDBSCAN believes these tickets belong together) counts toward confidence")
    confidence_weight_recency = models.FloatField(default=0.25, help_text="How much recent activity counts toward confidence")
    confidence_recency_window_days = models.PositiveSmallIntegerField(default=90, help_text="A ticket counts as 'recent' if created within this many days of now")
    confidence_floor = models.PositiveSmallIntegerField(default=30, help_text="Minimum possible confidence score — even the weakest real cluster shows at least this")
    confidence_cap = models.PositiveSmallIntegerField(default=97, help_text="Maximum possible confidence score — never a false-sounding 100%")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.color:
            self.color = random.choice(PALETTE)
        super().save(*args, **kwargs)

    @property
    def initials(self):
        words = [w for w in self.name.split() if w]
        letters = "".join(w[0] for w in words[:2]).upper()
        return letters or "PR"

    @property
    def ticket_count(self):
        return self.tickets.count()

    def cluster_count(self, engine=None):
        # Top-level only — drill-down sub-clusters aren't counted here, consistent
        # with the Problem Clusters list page and dashboard.
        qs = self.clusters.filter(is_noise=False, parent__isnull=True)
        if engine:
            qs = qs.filter(engine=engine)
        return qs.count()

    @property
    def cluster_count_ml(self):
        return self.cluster_count("traditional_ml")

    @property
    def cluster_count_genai(self):
        return self.cluster_count("generative_ai")
