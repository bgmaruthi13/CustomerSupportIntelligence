import re
from collections import Counter

import numpy as np
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models.functions import TruncMonth, TruncWeek
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from clustering.models import (Cluster, ClusterMember, ClusteringSettings,
                                DuplicateCandidate, GRANULARITY_CHOICES,
                                GlobalCluster, GlobalClusterMember, TicketEmbedding)
from clustering.pipelines import (DEFAULT_GRANULARITY, MIN_TICKETS_TO_CLUSTER,
                                   EmbeddingModelUnavailable, _get_embedding_model,
                                   _resolve_model_path, build_search_index,
                                   embedding_model_status, run_generative_ai,
                                   run_global_clustering, run_traditional_ml,
                                   scan_for_duplicates)
from clustering.settings_utils import default_settings_for
from clustering.settings_utils import get_or_default as get_clustering_settings
from clustering.text_utils import build_clustering_text, build_entity_stoplist
from core.models import Project
from core.utils import apply_sort, dumps_for_script, get_current_project
from tickets.models import MULTI_VALUE_DELIMITERS, Ticket, UploadBatch

SEARCH_RESULTS_LIMIT = 50

CLUSTERS_PER_PAGE = 25

CLUSTERS_SORT_FIELDS = {
    "name": "name",
    "recurring": "recurring_count",
    "confidence": "confidence",
    "trend": "trend",
    "application": "top_application",
    "offering": "top_offering",
    "country": "top_country",
    "created": "created_at",
}


@login_required
def clusters_list(request):
    project = get_current_project(request)
    context = {"active_nav": "clusters", "project": project}
    if not project:
        return render(request, "clustering/list.html", context)

    engine = request.GET.get("engine", "all")
    # Top-level only — sub-clusters (drill-down results) are reachable from their
    # parent's detail page, not mixed into this flat list.
    qs = Cluster.objects.filter(project=project, is_noise=False, parent__isnull=True)
    if engine in ("traditional_ml", "generative_ai"):
        qs = qs.filter(engine=engine)
    qs = qs.annotate(sub_cluster_count=Count("sub_clusters"))
    qs = apply_sort(request, qs, CLUSTERS_SORT_FIELDS, default_field="recurring", default_dir="desc")

    paginator = Paginator(qs, CLUSTERS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    ml_last = Cluster.objects.filter(project=project, engine="traditional_ml", parent__isnull=True).order_by("-created_at").first()
    genai_last = Cluster.objects.filter(project=project, engine="generative_ai", parent__isnull=True).order_by("-created_at").first()

    context.update({
        "clusters": page_obj,
        "page_obj": page_obj,
        "engine_filter": engine,
        "ml_count": Cluster.objects.filter(project=project, engine="traditional_ml", is_noise=False, parent__isnull=True).count(),
        "genai_count": Cluster.objects.filter(project=project, engine="generative_ai", is_noise=False, parent__isnull=True).count(),
        "problem_count": Cluster.objects.filter(project=project, is_problem_candidate=True, parent__isnull=True).count(),
        "total_recurring": qs.aggregate(total=Sum("recurring_count"))["total"] or 0,
        "ml_source": ml_last.source_description if ml_last else None,
        "genai_source": genai_last.source_description if genai_last else None,
        # A column showing "Unspecified" on every single row (e.g. offering
        # never mapped for this project) is pure dead weight — omit it rather
        # than always reserving table width for zero information.
        "show_application_col": qs.exclude(top_application="Unspecified").exists(),
        "show_offering_col": qs.exclude(top_offering="Unspecified").exists(),
        "show_country_col": qs.exclude(top_country="Unspecified").exists(),
    })
    return render(request, "clustering/list.html", context)


@login_required
def reset_clusters(request, engine):
    project = get_current_project(request)
    if request.method != "POST" or not project:
        return redirect("clustering:list")
    if engine not in ("traditional_ml", "generative_ai", "all"):
        return redirect("clustering:list")

    qs = Cluster.objects.filter(project=project)
    if engine != "all":
        qs = qs.filter(engine=engine)
    cluster_count = qs.count()
    qs.delete()

    label = {"traditional_ml": "Traditional ML", "generative_ai": "Generative AI", "all": "All engines'"}[engine]
    messages.success(request, f'Reset {label} clustering results for "{project.name}" — removed {cluster_count} clusters. Tickets and uploads are untouched; re-run the pipeline to regenerate.')

    if engine == "traditional_ml":
        return redirect("clustering:traditional_ml")
    if engine == "generative_ai":
        return redirect("clustering:generative_ai")
    return redirect("clustering:list")


def _build_copilot_prompt(cluster, members):
    """Server-rendered prompt text for the copy/paste bridge — never sent anywhere
    by this app itself, just generated for the user to paste into whichever Copilot
    (Desktop or VS Code) they already have open. Built from data already on the
    cluster/tickets, no extra query."""
    sample_titles = [m.ticket.title for m in members[:8]]
    lines = [
        "Here is a cluster of related IT support tickets. Suggest the likely root cause and a recommended resolution.",
        "",
        f"Cluster: {cluster.name}",
        f"Keywords: {cluster.keywords}",
        f"Recurring tickets: {cluster.recurring_count}",
        f"Trend: {cluster.get_trend_display()} — {cluster.trend_reasoning}" if cluster.trend_reasoning else f"Trend: {cluster.get_trend_display()}",
        f"Confidence: {cluster.confidence:.0f}%",
        f"Top country: {cluster.top_country}",
        f"Top application: {cluster.top_application}",
        f"Top offering: {cluster.top_offering or '—'}",
        "",
        "Sample ticket titles:",
    ]
    lines += [f"- {t}" for t in sample_titles]
    return "\n".join(lines)


def _build_summary_prompt(cluster, members):
    """Same copy/paste bridge as _build_copilot_prompt, asking for something
    different: not a root cause, just a one-line plain-English restatement of what
    this cluster is — the mechanical TF-IDF `keywords` field isn't a sentence a
    support manager would say out loud."""
    sample_titles = [m.ticket.title for m in members[:10]]
    lines = [
        "Here is a cluster of related IT support tickets. In ONE short, plain-English "
        "sentence — the kind a support manager would say out loud, not a list of "
        "keywords — summarize what problem this cluster represents.",
        "",
        f"Cluster keywords: {cluster.keywords}",
        f"Recurring tickets: {cluster.recurring_count}",
        f"Top application: {cluster.top_application}",
        "",
        "Sample ticket titles:",
    ]
    lines += [f"- {t}" for t in sample_titles]
    return "\n".join(lines)


def _build_trend_explanation_prompt(cluster, recent_members):
    """Same bridge again, this time asking for a hypothesis behind the trend label.
    trend_reasoning (see clustering.scoring.compute_trend) only states the count/
    date-window basis for Rising/Falling — never a cause. Built from the cluster's
    most-recent tickets specifically, since a cause for a *recent* shift is more
    likely visible in what's showing up lately than across the cluster's full
    history."""
    sample_titles = [m.ticket.title for m in recent_members[:10]]
    lines = [
        f"A cluster of related IT support tickets is currently trending "
        f"'{cluster.get_trend_display()}'. Basis for that label: {cluster.trend_reasoning}",
        "",
        "Based on the sample of its MOST RECENT ticket titles below, suggest a likely "
        "reason for this trend in 1-2 sentences (e.g. a shared vendor, a recent change, "
        "a seasonal pattern) — a hypothesis worth investigating, not a certainty.",
        "",
        "Most recent ticket titles:",
    ]
    lines += [f"- {t}" for t in sample_titles]
    return "\n".join(lines)


MEMBERS_SORT_FIELDS = {
    "title": "ticket__title",
    "created": "ticket__created_at",
    "similarity": "similarity",
    "country": "ticket__country",
    "application": "ticket__application",
}


@login_required
def cluster_detail(request, pk):
    project = get_current_project(request)
    cluster = get_object_or_404(Cluster, id=pk, project=project)
    # Always similarity-ranked regardless of the table's display sort — the Copilot
    # prompt's "sample ticket titles" should stay the most representative members,
    # not whatever a viewer happens to have the table sorted by right now.
    members_by_similarity = ClusterMember.objects.filter(cluster=cluster).select_related("ticket").order_by("-similarity")[:200]
    members = apply_sort(
        request,
        ClusterMember.objects.filter(cluster=cluster).select_related("ticket"),
        MEMBERS_SORT_FIELDS, default_field="similarity", default_dir="desc", param_prefix="members",
    )[:200]

    if request.method == "POST" and request.POST.get("action") == "save_resolution":
        notes = request.POST.get("resolution_notes", "").strip()
        source = request.POST.get("resolution_source", "manual")
        if source not in ("manual", "copilot_assisted"):
            source = "manual"
        cluster.resolution_notes = notes
        cluster.resolution_source = source if notes else ""
        cluster.resolution_added_by = request.user if notes else None
        cluster.resolution_added_at = timezone.now() if notes else None
        cluster.save(update_fields=["resolution_notes", "resolution_source", "resolution_added_by", "resolution_added_at"])
        messages.success(request, "Resolution notes saved.") if notes else messages.success(request, "Resolution notes cleared.")
        return redirect("clustering:detail", pk=cluster.id)

    if request.method == "POST" and request.POST.get("action") == "save_ai_summary":
        cluster.ai_summary = request.POST.get("ai_summary", "").strip()
        cluster.save(update_fields=["ai_summary"])
        messages.success(request, "AI summary saved.") if cluster.ai_summary else messages.success(request, "AI summary cleared.")
        return redirect("clustering:detail", pk=cluster.id)

    if request.method == "POST" and request.POST.get("action") == "save_trend_explanation":
        cluster.ai_trend_explanation = request.POST.get("ai_trend_explanation", "").strip()
        cluster.save(update_fields=["ai_trend_explanation"])
        messages.success(request, "Trend explanation saved.") if cluster.ai_trend_explanation else messages.success(request, "Trend explanation cleared.")
        return redirect("clustering:detail", pk=cluster.id)

    # Most-recent-first sample for the trend-explanation prompt — a cause for a
    # *recent* shift is more likely visible in what's showing up lately than across
    # the cluster's full history (which members_by_similarity/members represent).
    members_by_recency = ClusterMember.objects.filter(cluster=cluster).select_related("ticket").order_by("-ticket__created_at")[:10]

    tickets_qs = Ticket.objects.filter(cluster_memberships__cluster=cluster)
    by_month = (
        tickets_qs.annotate(bucket=TruncMonth("created_at")).values("bucket").annotate(count=Count("id")).order_by("bucket")
    )
    by_week = (
        tickets_qs.annotate(bucket=TruncWeek("created_at")).values("bucket").annotate(count=Count("id")).order_by("bucket")
    )
    by_country = tickets_qs.values("country").annotate(count=Count("id")).order_by("-count")

    offering_counts = Counter()
    for offering in tickets_qs.exclude(offering="").values_list("offering", flat=True):
        for part in MULTI_VALUE_DELIMITERS.split(offering):
            part = part.strip()
            if part:
                offering_counts[part] += 1

    context = {
        "active_nav": "clusters",
        "project": project,
        "cluster": cluster,
        "members": members,
        "copilot_prompt": _build_copilot_prompt(cluster, members_by_similarity),
        "summary_prompt": _build_summary_prompt(cluster, members_by_similarity),
        "trend_explanation_prompt": _build_trend_explanation_prompt(cluster, members_by_recency) if cluster.trend != "stable" else None,
        "keyword_list": [k.strip() for k in cluster.keywords.split(",") if k.strip()],
        "by_month_json": dumps_for_script([
            {"label": r["bucket"].strftime("%Y-%m") if r["bucket"] else "—", "count": r["count"]} for r in by_month
        ]),
        "by_week_json": dumps_for_script([
            {"label": r["bucket"].strftime("%Y-%m-%d") if r["bucket"] else "—", "count": r["count"]} for r in by_week
        ]),
        "by_country_json": dumps_for_script([{"label": r["country"] or "Unspecified", "count": r["count"]} for r in by_country]),
        "by_offering_json": dumps_for_script([{"label": k, "count": v} for k, v in offering_counts.most_common(12)]),
        "sub_clusters": apply_sort(
            request, cluster.sub_clusters.filter(is_noise=False), CLUSTERS_SORT_FIELDS,
            default_field="recurring", default_dir="desc", param_prefix="subclusters",
        ),
        "can_drill_down": cluster.recurring_count >= MIN_TICKETS_TO_CLUSTER,
        "granularity_choices": GRANULARITY_CHOICES,
    }
    return render(request, "clustering/detail.html", context)


@login_required
def drill_down(request, pk):
    """Re-clusters a cluster's own member tickets to reveal finer sub-groupings —
    scoped via `parent`, which _save_clusters uses to delete-and-rebuild only this
    cluster's prior children, never the project's other top-level clusters."""
    project = get_current_project(request)
    parent_cluster = get_object_or_404(Cluster, id=pk, project=project)
    if request.method != "POST":
        return redirect("clustering:detail", pk=parent_cluster.id)

    engine = request.POST.get("engine", parent_cluster.engine)
    granularity = request.POST.get("granularity", "fine")
    ticket_qs = Ticket.objects.filter(cluster_memberships__cluster=parent_cluster)
    description = f"Drill-down of '{parent_cluster.name}' ({ticket_qs.count()} tickets)"

    if engine == "traditional_ml":
        result = run_traditional_ml(project, ticket_queryset=ticket_qs, source_description=description, granularity=granularity, parent=parent_cluster)
    else:
        result = run_generative_ai(project, ticket_queryset=ticket_qs, source_description=description, granularity=granularity, parent=parent_cluster)

    messages.success(request, result.message) if result.ran else messages.warning(request, result.message)
    return redirect("clustering:detail", pk=parent_cluster.id)


RESULTS_PREVIEW_LIMIT = 25


MAX_OFFERING_OPTIONS = 30


def _offering_counts(project):
    """Explodes every ticket's (possibly multi-valued) offering field and tallies how
    many tickets carry each individual offering. A ticket tagged "A, B" counts toward
    both A and B."""
    counts = Counter()
    for offering in Ticket.objects.filter(project=project).exclude(offering="").values_list("offering", flat=True):
        for part in MULTI_VALUE_DELIMITERS.split(offering):
            part = part.strip()
            if part and part != "Unspecified":
                counts[part] += 1
    return counts


def _tickets_by_offering(project, offering_value):
    """Tickets whose offering field contains offering_value as one of its (possibly
    multiple, delimiter-separated) values — not just a substring match."""
    matching_ids = []
    for ticket_id, offering in Ticket.objects.filter(project=project).exclude(offering="").values_list("id", "offering"):
        parts = {p.strip() for p in MULTI_VALUE_DELIMITERS.split(offering) if p.strip()}
        if offering_value in parts:
            matching_ids.append(ticket_id)
    return Ticket.objects.filter(id__in=matching_ids)


def _dataset_options(project):
    """Selectable dataset scopes for this project: all tickets, each committed
    upload, tickets with no upload batch (e.g. seeded demo data), and — so pipelines
    can be scoped to a single offering — the most common individual offering values."""
    options = []
    all_count = Ticket.objects.filter(project=project).count()
    options.append({"value": "all", "label": f"All Tickets ({all_count})"})

    for batch in UploadBatch.objects.filter(project=project, status="committed", success_rows__gt=0).order_by("-created_at"):
        options.append({
            "value": f"batch:{batch.id}",
            "label": f"{batch.original_filename} — {batch.success_rows} tickets ({batch.created_at:%d %b %Y})",
        })

    no_batch_count = Ticket.objects.filter(project=project, upload_batch__isnull=True).count()
    if no_batch_count:
        options.append({"value": "no_batch", "label": f"Seeded / No Upload Source ({no_batch_count})"})

    for offering, count in _offering_counts(project).most_common(MAX_OFFERING_OPTIONS):
        options.append({"value": f"offering:{offering}", "label": f"Offering: {offering} ({count})"})

    return options


def _resolve_dataset_scope(project, scope_value):
    """Turns a scope selector value into a (queryset, human-readable description) pair."""
    if scope_value and scope_value.startswith("batch:"):
        batch_id = scope_value.split(":", 1)[1]
        batch = UploadBatch.objects.filter(id=batch_id, project=project).first()
        if batch:
            qs = Ticket.objects.filter(project=project, upload_batch=batch)
            return qs, f"{batch.original_filename} ({qs.count()} tickets, uploaded {batch.created_at:%d %b %Y})"

    if scope_value == "no_batch":
        qs = Ticket.objects.filter(project=project, upload_batch__isnull=True)
        return qs, f"Seeded / no upload source ({qs.count()} tickets)"

    if scope_value and scope_value.startswith("offering:"):
        offering_value = scope_value.split(":", 1)[1]
        qs = _tickets_by_offering(project, offering_value)
        return qs, f"Offering: {offering_value} ({qs.count()} tickets)"

    qs = Ticket.objects.filter(project=project)
    return qs, f"All {qs.count()} tickets"


def _pipeline_dataset_context(request, project, engine):
    tickets_qs = Ticket.objects.filter(project=project)
    ticket_count = tickets_qs.count()
    earliest = tickets_qs.order_by("created_at").first()
    latest = tickets_qs.order_by("-created_at").first()

    all_clusters = Cluster.objects.filter(project=project, engine=engine, is_noise=False)
    noise_cluster = Cluster.objects.filter(project=project, engine=engine, is_noise=True).first()
    last_run = Cluster.objects.filter(project=project, engine=engine).order_by("-created_at").first()

    cluster_count = all_clusters.count()
    problem_count = all_clusters.filter(is_problem_candidate=True).count()
    avg_confidence = all_clusters.aggregate(avg=Sum("confidence"))["avg"]
    avg_confidence = round(avg_confidence / cluster_count) if cluster_count else 0
    noise_count = noise_cluster.recurring_count if noise_cluster else 0
    noise_pct = round(noise_count / ticket_count * 100) if ticket_count else 0

    sorted_clusters = apply_sort(request, all_clusters, CLUSTERS_SORT_FIELDS, default_field="recurring", default_dir="desc")

    return {
        "ticket_count": ticket_count,
        "earliest": earliest.created_at if earliest else None,
        "latest": latest.created_at if latest else None,
        "clusters": sorted_clusters[:RESULTS_PREVIEW_LIMIT],
        "cluster_count": cluster_count,
        "problem_count": problem_count,
        "avg_confidence": avg_confidence,
        "noise_count": noise_count,
        "noise_pct": noise_pct,
        "show_application_col": all_clusters.exclude(top_application="Unspecified").exists(),
        "show_offering_col": all_clusters.exclude(top_offering="Unspecified").exists(),
        "last_run_at": last_run.created_at if last_run else None,
        "last_run_scope": last_run.source_description if last_run else None,
        "last_run_granularity": last_run.get_granularity_display() if last_run else None,
        "has_run": last_run is not None,
        "dataset_options": _dataset_options(project),
    }


@login_required
def traditional_ml(request):
    project = get_current_project(request)
    if request.method == "POST" and project:
        scope_value = request.POST.get("dataset_scope", "all")
        granularity = request.POST.get("granularity", DEFAULT_GRANULARITY)
        ticket_qs, description = _resolve_dataset_scope(project, scope_value)
        result = run_traditional_ml(project, ticket_queryset=ticket_qs, source_description=description, granularity=granularity)
        messages.success(request, result.message) if result.ran else messages.warning(request, result.message)
        return redirect("clustering:traditional_ml")

    context = {"active_nav": "traditional-ml", "project": project, "granularity_choices": GRANULARITY_CHOICES, "default_granularity": DEFAULT_GRANULARITY}
    if project:
        context.update(_pipeline_dataset_context(request, project, "traditional_ml"))
    return render(request, "clustering/traditional_ml.html", context)


@login_required
def generative_ai(request):
    project = get_current_project(request)
    if request.method == "POST" and project:
        scope_value = request.POST.get("dataset_scope", "all")
        granularity = request.POST.get("granularity", DEFAULT_GRANULARITY)
        ticket_qs, description = _resolve_dataset_scope(project, scope_value)
        result = run_generative_ai(project, ticket_queryset=ticket_qs, source_description=description, granularity=granularity)
        messages.success(request, result.message) if result.ran else messages.warning(request, result.message)
        return redirect("clustering:generative_ai")

    context = {"active_nav": "generative-ai", "project": project, "granularity_choices": GRANULARITY_CHOICES, "default_granularity": DEFAULT_GRANULARITY}
    if project:
        context.update(_pipeline_dataset_context(request, project, "generative_ai"))
    return render(request, "clustering/generative_ai.html", context)


MAX_EXPLORER_POINTS = 4000


@login_required
def explorer(request):
    import random

    project = get_current_project(request)
    context = {"active_nav": "explorer", "project": project}
    if not project:
        return render(request, "clustering/explorer.html", context)

    engine = request.GET.get("engine", "traditional_ml")
    base_qs = ClusterMember.objects.filter(cluster__project=project, cluster__engine=engine)
    total_points = base_qs.count()

    if total_points > MAX_EXPLORER_POINTS:
        all_ids = list(base_qs.values_list("id", flat=True))
        sampled_ids = random.Random(42).sample(all_ids, MAX_EXPLORER_POINTS)
        members = base_qs.filter(id__in=sampled_ids).select_related("cluster", "ticket")
    else:
        members = base_qs.select_related("cluster", "ticket")

    points = []
    clusters_seen = {}
    for m in members:
        c = m.cluster
        clusters_seen[c.id] = {"name": c.name, "is_noise": c.is_noise, "color": c.color if not c.is_noise else "#cbd5e1"}
        points.append({
            "x": m.x, "y": m.y, "z": m.z,
            "cluster": c.name,
            "cluster_id": c.id,
            "is_noise": c.is_noise,
            "ticket_id": m.ticket.external_id,
            "title": m.ticket.title[:80],
        })

    context.update({
        "engine": engine,
        "points_json": dumps_for_script(points),
        "point_count": len(points),
        "total_point_count": total_points,
        "is_sampled": total_points > MAX_EXPLORER_POINTS,
        "clusters_legend": list(clusters_seen.values()),
        "has_data": len(points) > 0,
    })
    return render(request, "clustering/explorer.html", context)


# Below this cosine-similarity percentage, a match is more "technically the
# closest thing we had" than a genuine hit — shown with the same visual weight
# as a 90%+ match otherwise, which risks a mediocre top result reading as a
# confident answer. Applies to Ask Correlate, Find Similar Tickets, and Quick
# Triage alike, since all three share _rank_by_vector.
WEAK_MATCH_THRESHOLD = 55


def _rank_by_vector(project, query_vec, top_k=SEARCH_RESULTS_LIMIT, exclude_ticket_id=None):
    """Cosine-ranks every TicketEmbedding in the project against a query vector and
    rolls the top matches up to their Generative AI cluster (or "Not yet clustered"
    if the ticket has an embedding but no cluster membership). Shared by Ask
    Correlate (query vector from typed text) and Find Similar Tickets (query vector
    from an existing ticket's own embedding) — no LLM call either way, ranking +
    Counter-based templated summary, same technique the EDA page already uses."""
    rows = list(TicketEmbedding.objects.filter(project=project).select_related("ticket"))
    if exclude_ticket_id is not None:
        rows = [r for r in rows if r.ticket_id != exclude_ticket_id]
    if not rows:
        return [], None

    matrix = np.vstack([np.frombuffer(r.vector, dtype=np.float32) for r in rows])
    query_vec = query_vec / (np.linalg.norm(query_vec) or 1.0)

    scores = matrix @ query_vec
    order = np.argsort(-scores)[:top_k]

    ticket_ids = [rows[i].ticket_id for i in order]
    membership_by_ticket = {
        m.ticket_id: m.cluster
        for m in ClusterMember.objects.filter(
            ticket_id__in=ticket_ids, cluster__engine="generative_ai", cluster__is_noise=False
        ).select_related("cluster")
    }

    results = []
    for i in order:
        row = rows[i]
        similarity = round(float(scores[i]) * 100, 1)
        results.append({
            "ticket": row.ticket,
            "similarity": similarity,
            "is_weak": similarity < WEAK_MATCH_THRESHOLD,
            "cluster": membership_by_ticket.get(row.ticket_id),
        })

    group_counts = Counter(r["cluster"].id if r["cluster"] else None for r in results)
    top_group_key, top_group_count = group_counts.most_common(1)[0]
    top_cluster = next((r["cluster"] for r in results if (r["cluster"].id if r["cluster"] else None) == top_group_key), None)

    country_counts = Counter(
        r["ticket"].country for r in results if r["ticket"].country and r["ticket"].country != "Unspecified"
    )
    top_countries = ", ".join(c for c, _ in country_counts.most_common(2))

    # The top result itself being weak means even the closest thing found isn't
    # a good match — the summary should hedge accordingly instead of stating a
    # confident-sounding conclusion off a mediocre best guess.
    if results and results[0]["is_weak"]:
        summary = f"No strong matches found — the closest is only {results[0]['similarity']:.0f}% similar."
    elif top_cluster:
        summary = f"{len(results)} matching tickets, mostly in cluster '{top_cluster.name}' ({top_cluster.trend}, {top_cluster.confidence:.0f}% confidence)"
    else:
        summary = f"{len(results)} matching tickets, mostly not yet clustered"
    if top_countries and not (results and results[0]["is_weak"]):
        summary += f", concentrated in {top_countries}"
    summary += "."

    return results, summary


SEARCH_RESULTS_SORT_KEYS = {
    "similarity": lambda r: r["similarity"],
    "title": lambda r: r["ticket"].title,
    "cluster": lambda r: r["cluster"].name if r["cluster"] else "",
}


def _sort_search_results(request, results):
    """Same ?sort=/&dir= contract as apply_sort, but for the plain Python list
    _rank_by_vector returns — it's cosine-ranked from a numpy matrix, not backed by a
    queryset, so it can't go through .order_by(). Sorting by similarity (the default)
    reproduces the existing rank order exactly, so this is safe to always apply."""
    sort_key = request.GET.get("sort")
    direction = request.GET.get("dir", "desc")
    key_func = SEARCH_RESULTS_SORT_KEYS.get(sort_key, SEARCH_RESULTS_SORT_KEYS["similarity"])
    return sorted(results, key=key_func, reverse=(direction != "asc"))


def _run_search(project, query, top_k=SEARCH_RESULTS_LIMIT):
    """Embeds free-typed query text, then ranks via _rank_by_vector."""
    model = _get_embedding_model()
    query_vec = model.encode([query])[0]
    return _rank_by_vector(project, query_vec, top_k=top_k)


def classify_text(project, text, top_k=5):
    """Quick single-text classification for the dashboard's Quick Triage widget:
    embeds arbitrary typed text on demand — NOT persisted, this is a scratch lookup
    for a ticket that may not exist in the system yet, not new data entering it — and
    returns the best-matching cluster plus a handful of supporting matches. Thin
    wrapper around the same _rank_by_vector ranking core Ask Correlate and Find
    Similar Tickets already use, so all three stay one implementation, not three."""
    if not text or not text.strip():
        return None
    if TicketEmbedding.objects.filter(project=project).count() == 0:
        return {"results": [], "summary": None}
    try:
        model = _get_embedding_model()
    except EmbeddingModelUnavailable as exc:
        return {"results": [], "summary": str(exc)}
    query_vec = model.encode([text])[0]
    results, summary = _rank_by_vector(project, query_vec, top_k=top_k)
    return {"results": results, "summary": summary}


QUEUE_SUGGESTION_K = 5
QUEUE_BULK_APPLY_THRESHOLD = 0.6
QUEUE_PAGE_LIMIT = 50


def _labeled_queue_matrix(project):
    """Every embedded ticket in the project that already has a real queue value —
    the labeled set the k-NN suggestion votes against. Built once per request, not
    once per candidate ticket, since re-fetching/re-stacking per ticket would be
    quadratic in the number of untriaged tickets."""
    rows = list(
        TicketEmbedding.objects.filter(project=project)
        .exclude(ticket__queue="Unspecified")
        .select_related("ticket")
    )
    if not rows:
        return None, None
    matrix = np.vstack([np.frombuffer(r.vector, dtype=np.float32) for r in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.where(norms == 0, 1.0, norms)
    queues = [r.ticket.queue for r in rows]
    return matrix, queues


def _suggest_queue(query_vec, matrix, queues, top_k=QUEUE_SUGGESTION_K):
    """Majority-vote queue suggestion among a ticket's nearest already-labeled
    neighbors — explainable (the agreement fraction IS the confidence), no training
    step, no LLM."""
    if matrix is None:
        return None
    query_vec = query_vec / (np.linalg.norm(query_vec) or 1.0)
    scores = matrix @ query_vec
    k = min(top_k, len(queues))
    order = np.argsort(-scores)[:k]
    neighbor_queues = [queues[i] for i in order]
    counts = Counter(neighbor_queues)
    suggested_queue, agree_count = counts.most_common(1)[0]
    return {
        "suggested_queue": suggested_queue,
        "agreement_fraction": agree_count / len(neighbor_queues),
        "agree_count": agree_count,
        "neighbor_count": len(neighbor_queues),
    }


def _queue_suggestions(project, limit=QUEUE_PAGE_LIMIT):
    """Untriaged-ticket count plus up to `limit` suggestions. Embeds any untriaged
    tickets missing a TicketEmbedding in a single batched call rather than one
    encode() per ticket."""
    untriaged_qs = Ticket.objects.filter(project=project, queue="Unspecified")
    untriaged_count = untriaged_qs.count()
    page_tickets = list(untriaged_qs[:limit])

    matrix, queues = _labeled_queue_matrix(project)
    suggestions = []
    if matrix is not None and page_tickets:
        embedding_by_ticket = {
            e.ticket_id: np.frombuffer(e.vector, dtype=np.float32)
            for e in TicketEmbedding.objects.filter(ticket__in=page_tickets)
        }
        missing = [t for t in page_tickets if t.id not in embedding_by_ticket]
        if missing:
            from clustering.pipelines import _embed_tickets, _persist_embeddings
            try:
                new_embeddings, _ = _embed_tickets(missing, project)
            except EmbeddingModelUnavailable:
                new_embeddings = []
            _persist_embeddings(project, missing, new_embeddings) if new_embeddings else None
            for ticket, vec in zip(missing, new_embeddings):
                embedding_by_ticket[ticket.id] = vec

        for ticket in page_tickets:
            suggestion = _suggest_queue(embedding_by_ticket[ticket.id], matrix, queues)
            if suggestion:
                suggestions.append({"ticket": ticket, **suggestion})

    return untriaged_count, suggestions


SUGGESTIONS_SORT_KEYS = {
    "ticket": lambda s: s["ticket"].external_id,
    "queue": lambda s: s["suggested_queue"],
    "agreement": lambda s: s["agreement_fraction"],
}


def _sort_suggestions(request, suggestions):
    """Same ?sort=/&dir= contract as apply_sort, but for a plain Python list — the
    k-NN suggestion list isn't backed by a queryset (it's assembled per-request from
    embedding math), so it can't go through .order_by()."""
    sort_key = request.GET.get("sort")
    direction = request.GET.get("dir", "desc")
    key_func = SUGGESTIONS_SORT_KEYS.get(sort_key, SUGGESTIONS_SORT_KEYS["agreement"])
    return sorted(suggestions, key=key_func, reverse=(direction != "asc"))


@login_required
def categorize_tickets(request):
    project = get_current_project(request)
    context = {"active_nav": "categorize", "project": project}
    if not project:
        return render(request, "clustering/categorize.html", context)

    if request.method == "POST":
        action = request.POST.get("action", "apply_one")
        if action == "bulk_apply":
            _, suggestions = _queue_suggestions(project)
            applied = 0
            for s in suggestions:
                if s["agreement_fraction"] >= QUEUE_BULK_APPLY_THRESHOLD:
                    s["ticket"].queue = s["suggested_queue"]
                    s["ticket"].queue_source = "suggested"
                    s["ticket"].save(update_fields=["queue", "queue_source"])
                    applied += 1
            if applied:
                messages.success(request, f"Applied {applied} high-agreement suggestion{'s' if applied != 1 else ''} (≥{int(QUEUE_BULK_APPLY_THRESHOLD * 100)}% neighbor agreement).")
            else:
                messages.warning(request, "No suggestions met the high-agreement bar — nothing applied.")
        else:
            ticket_id = request.POST.get("ticket_id")
            queue_value = request.POST.get("queue_value", "").strip()
            if ticket_id and queue_value:
                ticket = get_object_or_404(Ticket, id=ticket_id, project=project)
                ticket.queue = queue_value
                ticket.queue_source = "suggested"
                ticket.save(update_fields=["queue", "queue_source"])
                messages.success(request, f'{ticket.external_id} categorized as "{queue_value}".')
        return redirect("clustering:categorize")

    untriaged_count, suggestions = _queue_suggestions(project)
    suggestions = _sort_suggestions(request, suggestions)
    context.update({
        "untriaged_count": untriaged_count,
        "shown_count": len(suggestions),
        "suggestions": suggestions,
        "needs_index": TicketEmbedding.objects.filter(project=project).count() == 0,
        "bulk_apply_threshold_pct": int(QUEUE_BULK_APPLY_THRESHOLD * 100),
    })
    return render(request, "clustering/categorize.html", context)


@login_required
def search(request):
    project = get_current_project(request)
    context = {"active_nav": "search", "project": project}
    if not project:
        return render(request, "clustering/search.html", context)

    if request.method == "POST" and request.POST.get("action") == "build_index":
        result = build_search_index(project)
        messages.success(request, result.message) if result.ran else messages.warning(request, result.message)
        return redirect("clustering:search")

    total_indexed = TicketEmbedding.objects.filter(project=project).count()
    total_tickets = Ticket.objects.filter(project=project).count()

    model_available, model_path_or_error = embedding_model_status()
    stale_model = (
        model_available and total_indexed > 0
        and TicketEmbedding.objects.filter(project=project).exclude(model_path=model_path_or_error).exists()
    )

    query = request.GET.get("q", "").strip()
    results, summary = (None, None)
    if query and total_indexed and not stale_model and model_available:
        results, summary = _run_search(project, query)
        if results:
            results = _sort_search_results(request, results)

    context.update({
        "query": query,
        "results": results,
        "summary": summary,
        "total_indexed": total_indexed,
        "total_tickets": total_tickets,
        "needs_index": total_indexed == 0,
        "index_stale_count": max(0, total_tickets - total_indexed) if total_indexed else 0,
        "stale_model": stale_model,
        "embedding_unavailable": None if model_available else model_path_or_error,
    })
    return render(request, "clustering/search.html", context)


@login_required
def similar_tickets(request, ticket_id):
    """Find Similar Tickets — nearest-neighbor lookup off one specific ticket rather
    than typed text. Reuses the ticket's own TicketEmbedding as the query vector; if
    it doesn't have one yet (project never ran/indexed Generative AI), embeds this one
    ticket on demand rather than blocking on a full project reindex."""
    project = get_current_project(request)
    ticket = get_object_or_404(Ticket, id=ticket_id, project=project)

    embedding_row = TicketEmbedding.objects.filter(ticket=ticket).first()
    if embedding_row is None:
        from clustering.pipelines import _embed_tickets, _persist_embeddings
        try:
            embeddings, _ = _embed_tickets([ticket], project)
        except EmbeddingModelUnavailable as exc:
            messages.error(request, str(exc))
            return redirect("clustering:detail", pk=ticket.cluster_memberships.first().cluster_id) if ticket.cluster_memberships.exists() else redirect("clustering:list")
        _persist_embeddings(project, [ticket], embeddings)
        embedding_row = TicketEmbedding.objects.get(ticket=ticket)

    query_vec = np.frombuffer(embedding_row.vector, dtype=np.float32)
    results, summary = _rank_by_vector(project, query_vec, exclude_ticket_id=ticket.id)
    if results:
        results = _sort_search_results(request, results)

    context = {
        "active_nav": "search",
        "project": project,
        "source_ticket": ticket,
        "query": "",
        "results": results,
        "summary": summary,
        "total_indexed": TicketEmbedding.objects.filter(project=project).count(),
        "total_tickets": Ticket.objects.filter(project=project).count(),
        "needs_index": False,
        "index_stale_count": 0,
        "stale_model": False,
    }
    return render(request, "clustering/search.html", context)


DUPLICATES_SORT_FIELDS = {
    "similarity": "similarity",
    "ticket_a": "ticket_a__external_id",
    "created": "created_at",
}

DUPLICATES_BULK_CONFIRM_THRESHOLD = 95


@login_required
def duplicates(request):
    project = get_current_project(request)
    context = {"active_nav": "duplicates", "project": project}
    if not project:
        return render(request, "clustering/duplicates.html", context)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "scan":
            found = scan_for_duplicates(project)
            if found:
                messages.success(request, f"Scan complete — found {found} new possible duplicate pair{'s' if found != 1 else ''}.")
            else:
                messages.info(request, "Scan complete — no new possible duplicates found.")
        elif action in ("confirm", "dismiss"):
            pair = get_object_or_404(DuplicateCandidate, id=request.POST.get("pair_id"), ticket_a__project=project)
            pair.status = "confirmed" if action == "confirm" else "dismissed"
            pair.reviewed_by = request.user
            pair.reviewed_at = timezone.now()
            pair.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        elif action == "bulk_confirm":
            updated = DuplicateCandidate.objects.filter(
                ticket_a__project=project, status="pending",
                similarity__gte=DUPLICATES_BULK_CONFIRM_THRESHOLD,
            ).update(status="confirmed", reviewed_by=request.user, reviewed_at=timezone.now())
            if updated:
                messages.success(request, f"Confirmed {updated} pair{'s' if updated != 1 else ''} at ≥{DUPLICATES_BULK_CONFIRM_THRESHOLD}% similarity.")
            else:
                messages.warning(request, f"No pending pairs met the ≥{DUPLICATES_BULK_CONFIRM_THRESHOLD}% bar — nothing confirmed.")
        return redirect("clustering:duplicates")

    pending = (
        DuplicateCandidate.objects.filter(ticket_a__project=project, status="pending")
        .select_related("ticket_a", "ticket_b")
    )
    pending = apply_sort(request, pending, DUPLICATES_SORT_FIELDS, default_field="similarity", default_dir="desc")
    reviewed_count = DuplicateCandidate.objects.filter(ticket_a__project=project).exclude(status="pending").count()

    bulk_eligible_count = pending.filter(similarity__gte=DUPLICATES_BULK_CONFIRM_THRESHOLD).count()

    context.update({
        "pending": pending,
        "pending_count": pending.count(),
        "reviewed_count": reviewed_count,
        "total_indexed": TicketEmbedding.objects.filter(project=project).count(),
        "bulk_confirm_threshold": DUPLICATES_BULK_CONFIRM_THRESHOLD,
        "bulk_eligible_count": bulk_eligible_count,
    })
    return render(request, "clustering/duplicates.html", context)


GLOBAL_CLUSTERS_SORT_FIELDS = {
    "name": "name",
    "recurring": "recurring_count",
    "confidence": "confidence",
    "projects": "project_count",
    "trend": "trend",
}


@login_required
def global_clustering(request):
    """Cross-project clustering — the "how does this look across everything we've
    uploaded" report, for showing a stakeholder patterns no single project's own
    clustering could surface. Scoped to the current user's own projects, same
    ownership model the Projects page already uses (Project.objects.filter(
    owner=request.user)) — this is a reporting view over the projects you have, not a
    break of that ownership boundary."""
    projects = Project.objects.filter(owner=request.user).order_by("name")

    if request.method == "POST":
        selected_ids = [int(pid) for pid in request.POST.getlist("project_ids")]
        engine = request.POST.get("engine", "traditional_ml")
        granularity = request.POST.get("granularity", DEFAULT_GRANULARITY)
        if len(selected_ids) < 2:
            messages.warning(request, "Select at least 2 projects — clustering a single project's own tickets is what its Traditional ML / Generative AI page already does.")
        else:
            # Ownership check: only cluster projects this user actually owns, even if
            # a stray/forged id shows up in the POST data.
            owned_ids = list(projects.filter(id__in=selected_ids).values_list("id", flat=True))
            result = run_global_clustering(engine, project_ids=owned_ids, granularity=granularity, run_by=request.user)
            messages.success(request, result.message) if result.ran else messages.warning(request, result.message)
        return redirect(f"{reverse('clustering:global_clustering')}?engine={engine}")

    engine = request.GET.get("engine", "traditional_ml")
    clusters_qs = GlobalCluster.objects.filter(engine=engine, is_noise=False, run_by=request.user)
    clusters_qs = apply_sort(request, clusters_qs, GLOBAL_CLUSTERS_SORT_FIELDS, default_field="projects", default_dir="desc")
    intersections = clusters_qs.filter(is_significant_intersection=True)
    last_run = GlobalCluster.objects.filter(engine=engine, run_by=request.user).order_by("-run_at").first()

    intersection_data = []
    for cluster in intersections:
        breakdown = (
            GlobalClusterMember.objects.filter(cluster=cluster)
            .values("project__name", "project__color")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        intersection_data.append({"cluster": cluster, "breakdown": list(breakdown)})

    context = {
        "active_nav": "global-clustering",
        "projects": projects,
        "engine": engine,
        "granularity_choices": GRANULARITY_CHOICES,
        "default_granularity": DEFAULT_GRANULARITY,
        "clusters": clusters_qs,
        "cluster_count": clusters_qs.count(),
        "intersection_data": intersection_data,
        "intersection_count": intersections.count(),
        "has_run": last_run is not None,
        "last_run_at": last_run.run_at if last_run else None,
        "last_run_scope": last_run.source_description if last_run else None,
        "last_run_by": last_run.run_by if last_run else None,
    }
    return render(request, "clustering/global_clustering.html", context)


GLOBAL_MEMBERS_SORT_FIELDS = {
    "project": "project__name",
    "title": "ticket__title",
    "created": "ticket__created_at",
    "similarity": "similarity",
}


@login_required
def global_cluster_detail(request, pk):
    """The actual destination for "View clusters →" on the Global Clustering page.
    Previously that link pointed at the regular per-project cluster list
    (clustering:list), which has no way to show a GlobalCluster's members at all —
    they're a different model, spanning multiple projects, that clustering:list never
    queries. This shows the cluster's own composition instead."""
    cluster = get_object_or_404(GlobalCluster, id=pk, run_by=request.user)
    members = apply_sort(
        request,
        GlobalClusterMember.objects.filter(cluster=cluster).select_related("ticket", "project"),
        GLOBAL_MEMBERS_SORT_FIELDS, default_field="project", default_dir="asc",
    )
    breakdown = (
        GlobalClusterMember.objects.filter(cluster=cluster)
        .values("project__id", "project__name", "project__color")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    context = {
        "active_nav": "global-clustering",
        "cluster": cluster,
        "members": members[:200],
        "member_count": members.count(),
        "breakdown": breakdown,
    }
    return render(request, "clustering/global_cluster_detail.html", context)


@login_required
def global_explorer(request):
    """2D/3D projection for a global run — same UMAP-projected coordinates
    _save_global_clusters already persists on GlobalClusterMember, same Plotly
    rendering pattern as the per-project Cluster Explorer. The one addition: a "color
    by project" mode plus an intersection-highlight toggle, since the whole point of a
    global run is spotting where different projects' tickets land in the same
    neighborhood — coloring by cluster alone (the per-project Explorer's only mode)
    doesn't make that visible."""
    import random

    engine = request.GET.get("engine", "traditional_ml")
    base_qs = GlobalClusterMember.objects.filter(cluster__engine=engine, cluster__run_by=request.user)
    total_points = base_qs.count()

    if total_points > MAX_EXPLORER_POINTS:
        all_ids = list(base_qs.values_list("id", flat=True))
        sampled_ids = random.Random(42).sample(all_ids, MAX_EXPLORER_POINTS)
        members = base_qs.filter(id__in=sampled_ids).select_related("cluster", "ticket", "project")
    else:
        members = base_qs.select_related("cluster", "ticket", "project")

    points = []
    clusters_seen = {}
    projects_seen = {}
    for m in members:
        c, p = m.cluster, m.project
        clusters_seen[c.id] = {"name": c.name, "is_noise": c.is_noise, "color": c.color if not c.is_noise else "#cbd5e1"}
        projects_seen[p.id] = {"name": p.name, "color": p.color}
        points.append({
            "x": m.x, "y": m.y, "z": m.z,
            "cluster": c.name,
            "cluster_id": c.id,
            "is_noise": c.is_noise,
            "is_intersection": c.is_significant_intersection,
            "project": p.name,
            "project_id": p.id,
            "ticket_id": m.ticket.external_id,
            "title": m.ticket.title[:80],
        })

    context = {
        "active_nav": "global-clustering",
        "engine": engine,
        "points_json": dumps_for_script(points),
        "point_count": len(points),
        "total_point_count": total_points,
        "is_sampled": total_points > MAX_EXPLORER_POINTS,
        "clusters_legend": list(clusters_seen.values()),
        "projects_legend": list(projects_seen.values()),
        "has_data": len(points) > 0,
    }
    return render(request, "clustering/global_explorer.html", context)


# Source fields a project's tickets actually have real text/categorical signal on —
# external_id/created_at are identifiers/timestamps, not clustering-relevant content.
# (field, label, recommended) — title/description are "recommended" per the design's
# resolution of the "restrict to title/description or any field?" open question:
# allow any field, but steer users toward the two that usually carry the most signal.
SOURCE_FIELD_CHOICES = [
    ("title", "Title", True),
    ("description", "Description", True),
    ("country", "Country", False),
    ("application", "Application", False),
    ("status", "Status", False),
    ("priority", "Priority", False),
    ("offering", "Offering", False),
    ("requested_type", "Requested Type", False),
    ("assigned_to", "Assigned To", False),
    ("created_by", "Created By", False),
    ("queue", "Queue", False),
]


def _parse_pattern_lines(raw_text):
    """One pattern per line. A line prefixed "regex:" opts that single entry into
    regex matching; every other line is a plain phrase — the open-question
    resolution of "plain-phrase by default, opt-in regex per entry" without needing
    a repeatable-row JS widget to express a per-entry toggle."""
    patterns = []
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("regex:"):
            patterns.append({"pattern": line[len("regex:"):].strip(), "is_regex": True})
        else:
            patterns.append({"pattern": line, "is_regex": False})
    return patterns


def _patterns_to_lines(patterns):
    return "\n".join(
        (f"regex: {p.get('pattern', '')}" if p.get("is_regex") else p.get("pattern", ""))
        for p in (patterns or [])
    )


def _parse_word_list(raw_text):
    return sorted({w.strip().lower() for w in re.split(r"[,\n]", raw_text or "") if w.strip()})


def _settings_from_post(request, project, engine):
    """Builds an updated (not-yet-saved) ClusteringSettings from this engine's
    submitted form — starts from the existing saved row if one exists (so fields
    outside this form, if any get added later, aren't clobbered back to defaults),
    otherwise from this engine's default instance."""
    existing = ClusteringSettings.objects.filter(project=project, engine=engine).first()
    obj = existing or default_settings_for(project, engine)
    obj.project = project
    obj.engine = engine
    obj.source_fields = request.POST.getlist("source_fields") or ["title"]
    obj.normalize_whitespace = request.POST.get("normalize_whitespace") == "on"
    obj.strip_html = request.POST.get("strip_html") == "on"
    obj.remove_urls = request.POST.get("remove_urls") == "on"
    obj.remove_emails = request.POST.get("remove_emails") == "on"
    obj.strip_boilerplate = request.POST.get("strip_boilerplate") == "on"
    obj.boilerplate_patterns = _parse_pattern_lines(request.POST.get("boilerplate_patterns", ""))
    obj.normalize_unicode = request.POST.get("normalize_unicode") == "on"
    obj.strip_ticket_id_patterns = request.POST.get("strip_ticket_id_patterns") == "on"
    obj.ticket_id_patterns = _parse_pattern_lines(request.POST.get("ticket_id_patterns", ""))
    obj.lowercase = request.POST.get("lowercase") == "on"
    obj.keep_only_text = request.POST.get("keep_only_text") == "on"
    obj.remove_special_characters = request.POST.get("remove_special_characters") == "on"
    obj.remove_numbers = request.POST.get("remove_numbers") == "on"
    obj.remove_stopwords = request.POST.get("remove_stopwords") == "on"
    obj.stopwords_add = _parse_word_list(request.POST.get("stopwords_add", ""))
    obj.stopwords_exclude = _parse_word_list(request.POST.get("stopwords_exclude", ""))
    try:
        obj.min_token_length = max(1, int(request.POST.get("min_token_length", 1)))
    except (TypeError, ValueError):
        obj.min_token_length = 1
    obj.enable_stemming = request.POST.get("enable_stemming") == "on"
    try:
        obj.min_document_frequency = max(1, int(request.POST.get("min_document_frequency", 2)))
    except (TypeError, ValueError):
        obj.min_document_frequency = 2
    try:
        obj.max_document_frequency = min(1.0, max(0.01, float(request.POST.get("max_document_frequency", 0.6))))
    except (TypeError, ValueError):
        obj.max_document_frequency = 0.6
    return obj


@login_required
def clustering_settings(request):
    """Per-project Clustering Settings — source column selection, concatenation, and
    the configurable preprocessing pipeline described in BACKLOG.md. Traditional ML
    and Generative AI are independently configurable (two separate forms on one
    page); saving one never touches the other. Changing settings never re-runs a
    pipeline automatically — the next manual "Run Traditional ML"/"Run Generative AI"
    picks up the new config, consistent with how granularity changes already work."""
    project = get_current_project(request)
    context = {"active_nav": "clustering-settings", "project": project}
    if not project:
        return render(request, "clustering/settings.html", context)

    tab = request.GET.get("tab", "traditional_ml")
    submitted_engine = None
    submitted_settings = None
    preview = None

    if request.method == "POST":
        engine = request.POST.get("engine")
        action = request.POST.get("action")
        if action == "save_embedding_path":
            from core.models import SiteSettings
            new_path = request.POST.get("embedding_model_path", "").strip()
            site_settings = SiteSettings.load()
            site_settings.embedding_model_path = new_path
            site_settings.save(update_fields=["embedding_model_path"])
            available, path_or_error = embedding_model_status()
            if available:
                messages.success(request, f"Saved — embedding model resolved at ‘{path_or_error}’.")
            else:
                messages.error(request, f"Saved, but this path doesn't resolve to a real folder: {path_or_error}")
            return redirect(f"{reverse('clustering:settings')}?tab={tab}")
        if action == "save_confidence":
            try:
                project.confidence_weight_size = max(0.0, float(request.POST.get("confidence_weight_size", 40)))
                project.confidence_weight_density = max(0.0, float(request.POST.get("confidence_weight_density", 35)))
                project.confidence_weight_recency = max(0.0, float(request.POST.get("confidence_weight_recency", 25)))
                project.confidence_recency_window_days = max(1, int(request.POST.get("confidence_recency_window_days", 90)))
                floor = max(0, min(100, int(request.POST.get("confidence_floor", 30))))
                cap = max(0, min(100, int(request.POST.get("confidence_cap", 97))))
                if floor >= cap:
                    messages.error(request, "Confidence floor must be lower than the cap — settings not saved.")
                else:
                    project.confidence_floor = floor
                    project.confidence_cap = cap
                    project.save(update_fields=[
                        "confidence_weight_size", "confidence_weight_density", "confidence_weight_recency",
                        "confidence_recency_window_days", "confidence_floor", "confidence_cap",
                    ])
                    messages.success(request, "Saved Confidence Scoring settings. Nothing re-runs automatically — the next pipeline run for either engine will use the new weights.")
            except (TypeError, ValueError):
                messages.error(request, "Confidence Scoring values must be numbers — settings not saved.")
            return redirect(f"{reverse('clustering:settings')}?tab={tab}")
        if engine in ("traditional_ml", "generative_ai"):
            tab = engine
            submitted_engine = engine
            submitted_settings = _settings_from_post(request, project, engine)
            if action == "save":
                submitted_settings.save()
                messages.success(
                    request,
                    f"Saved {dict(ClusteringSettings._meta.get_field('engine').choices).get(engine, engine)} "
                    "clustering settings. Nothing re-runs automatically — the next pipeline run for this "
                    "engine will use the new settings.",
                )
                return redirect(f"{reverse('clustering:settings')}?tab={engine}")
            elif action == "preview":
                sample_ticket = Ticket.objects.filter(project=project).order_by("-created_at").first()
                if sample_ticket:
                    stoplist = build_entity_stoplist([sample_ticket])
                    raw_fields = submitted_settings.source_fields or ["title"]
                    raw_text = " ".join(str(getattr(sample_ticket, f, "") or "") for f in raw_fields).strip()
                    preview = {
                        "ticket": sample_ticket,
                        "raw_text": raw_text,
                        "result_text": build_clustering_text(sample_ticket, stoplist, submitted_settings),
                    }
                else:
                    messages.warning(request, "No tickets in this project yet to preview against.")

    ml_settings = submitted_settings if submitted_engine == "traditional_ml" else get_clustering_settings(project, "traditional_ml")
    genai_settings = submitted_settings if submitted_engine == "generative_ai" else get_clustering_settings(project, "generative_ai")

    from core.models import SiteSettings
    embedding_available, embedding_path_or_error = embedding_model_status()

    context.update({
        "tab": tab,
        "source_field_choices": SOURCE_FIELD_CHOICES,
        "ml_settings": ml_settings,
        "genai_settings": genai_settings,
        "ml_boilerplate_text": _patterns_to_lines(ml_settings.boilerplate_patterns),
        "ml_ticket_id_text": _patterns_to_lines(ml_settings.ticket_id_patterns),
        "genai_boilerplate_text": _patterns_to_lines(genai_settings.boilerplate_patterns),
        "genai_ticket_id_text": _patterns_to_lines(genai_settings.ticket_id_patterns),
        "preview": preview if submitted_engine == tab else None,
        "preview_engine": submitted_engine,
        "embedding_model_path": SiteSettings.load().embedding_model_path,
        "embedding_available": embedding_available,
        "embedding_status_message": embedding_path_or_error,
    })
    return render(request, "clustering/settings.html", context)
