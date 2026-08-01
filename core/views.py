from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from clustering.models import Cluster
from clustering.views import CLUSTERS_SORT_FIELDS
from core.models import Project
from core.utils import apply_sort, get_current_project
from tickets.models import Ticket


def healthz(request):
    """Unauthenticated liveness/readiness probe for load balancers and uptime monitors."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception as exc:  # noqa: BLE001 — surface any DB failure to the prober
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)


def _build_dashboard_narrative_prompt(project, ticket_count, clusters, problem_candidates, total_quarterly_cost, cost_currency_symbol):
    """A leadership-facing 'what's going on right now' briefing (BACKLOG,
    user-requested wow-factor idea #1) — reads the same numbers already on
    the dashboard (nothing extra queried) and turns them into 3-4 sentences,
    the way an analyst would open a status update, not a data dump."""
    lines = [
        "You are writing a status briefing for engineering leadership, based ONLY on "
        "the real data below. 5-7 sentences, plain English, the way an analyst would "
        "open a status update out loud - not a bulleted data dump. Name the SPECIFIC "
        "top clusters by name and cite their actual numbers (recurring count, trend) "
        "rather than speaking in generalities, and don't invent numbers not given "
        "here.",
        "",
        f"Project: {project.name}" + (f" ({project.domain})" if project.domain else ""),
        f"Total tickets: {ticket_count}",
        f"Active problem clusters: {len(clusters)} shown below, {problem_candidates} flagged as Problem Candidates",
    ]
    if total_quarterly_cost:
        lines.append(f"Estimated cost of unaddressed problem clusters: {cost_currency_symbol}{total_quarterly_cost:,.0f}/quarter at the current rate")
    lines.append("")
    lines.append("Top clusters (name, recurring count, trend, confidence):")
    for c in clusters:
        lines.append(f"- {c.name}: {c.recurring_count} tickets, {c.get_trend_display()}, {c.confidence:.0f}% confidence")
    return "\n".join(lines)


@login_required
def dashboard(request):
    project = get_current_project(request)
    context = {"active_nav": "dashboard"}
    if not project:
        return render(request, "core/dashboard.html", context)

    classify_text_input = ""
    classify_result = None
    if request.method == "POST" and request.POST.get("action") == "classify":
        from clustering.views import classify_text
        classify_text_input = request.POST.get("classify_text", "").strip()
        classify_result = classify_text(project, classify_text_input)

    # Top-level clusters only — drill-down sub-clusters are reachable from their
    # parent's detail page, not counted or listed here, consistent with clusters_list.
    clusters_qs = Cluster.objects.filter(project=project, is_noise=False, parent__isnull=True)
    clusters_qs = apply_sort(request, clusters_qs, CLUSTERS_SORT_FIELDS, default_field="recurring", default_dir="desc")
    clusters = clusters_qs[:8]
    problem_candidates = Cluster.objects.filter(project=project, is_problem_candidate=True, parent__isnull=True).count()

    # Aggregate dollar-impact KPI (BACKLOG B.1) — summed across problem
    # candidates specifically, not every cluster: this is meant to answer
    # "what's the cost of the recurring issues we haven't addressed", and a
    # stable/already-understood cluster isn't part of that story.
    total_quarterly_cost = None
    if project.cost_per_ticket:
        from clustering.views import annotate_recent_ticket_count, COST_WINDOW_DAYS
        from clustering.scoring import estimate_quarterly_cost
        problem_qs = annotate_recent_ticket_count(
            Cluster.objects.filter(project=project, is_problem_candidate=True, parent__isnull=True)
        )
        recent_total = sum(c.recent_ticket_count for c in problem_qs)
        total_quarterly_cost = estimate_quarterly_cost(recent_total, project.cost_per_ticket, COST_WINDOW_DAYS)

    dashboard_narrative = None
    if request.GET.get("narrate") == "1":
        from core.llm_bridge import LLMBridgeUnavailable, ask
        try:
            dashboard_narrative = ask(_build_dashboard_narrative_prompt(
                project, project.ticket_count, clusters, problem_candidates, total_quarterly_cost, project.cost_currency_symbol,
            ))
        except LLMBridgeUnavailable as exc:
            messages.error(request, f"Couldn't generate a briefing: {exc}")

    context.update({
        "project": project,
        "ticket_count": project.ticket_count,
        "cluster_count": Cluster.objects.filter(project=project, is_noise=False, parent__isnull=True).count(),
        "ml_cluster_count": Cluster.objects.filter(project=project, engine="traditional_ml", is_noise=False, parent__isnull=True).count(),
        "genai_cluster_count": Cluster.objects.filter(project=project, engine="generative_ai", is_noise=False, parent__isnull=True).count(),
        "problem_candidates": problem_candidates,
        "clusters": clusters,
        "classify_text_input": classify_text_input,
        "classify_result": classify_result,
        "total_quarterly_cost": total_quarterly_cost,
        "cost_currency_symbol": project.cost_currency_symbol,
        "dashboard_narrative": dashboard_narrative,
    })
    return render(request, "core/dashboard.html", context)


PROJECTS_SORT_FIELDS = {
    "name": "name",
    "tickets": "ticket_total",
    "created": "created_at",
}


@login_required
def projects(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        domain = request.POST.get("domain", "").strip()
        if not name:
            messages.error(request, "Project name is required.")
        else:
            project = Project.objects.create(name=name, domain=domain or "General ITSM", owner=request.user)
            request.session["current_project_id"] = project.id
            messages.success(request, f'Project "{project.name}" created.')
            return redirect("dashboard")

    project_list = Project.objects.filter(owner=request.user).annotate(ticket_total=Count("tickets"))
    project_list = apply_sort(request, project_list, PROJECTS_SORT_FIELDS, default_field="created", default_dir="desc")
    return render(request, "core/projects.html", {
        "active_nav": "projects",
        "projects_list": project_list,
    })


@login_required
def switch_project(request, pk):
    project = get_object_or_404(Project, id=pk, owner=request.user)
    request.session["current_project_id"] = project.id
    return redirect(request.GET.get("next") or "dashboard")


def platform_story(request):
    """Public, unauthenticated 'what is this' page (user-requested leadership-
    facing redesign) — the front door someone hits before ever logging in.
    Distinct from how_it_works (the in-app, login-required technical
    reference): this is the pitch, not the manual. No aspirational claims —
    every capability named here is something actually built and verified in
    this session, not a roadmap item."""
    return render(request, "core/platform_story.html")


@login_required
def how_it_works(request):
    """Single consolidated reference page: how the platform works, its two engines,
    assumptions, known limitations, live KPI snapshot, and the original proposal's
    plan/roadmap — replaces the four separate Comparison/Project Plan/KPIs/Roadmap pages."""
    project = get_current_project(request)
    context = {"active_nav": "how-it-works", "project": project}
    if project:
        total = Ticket.objects.filter(project=project).count()
        clusters_qs = Cluster.objects.filter(project=project, is_noise=False)
        cluster_count = clusters_qs.count()
        # Distinct tickets covered by *either* engine — summing recurring_count across
        # both engines double-counts any ticket clustered by both, which was driving
        # this to 0% (falsely implying zero manual effort remains) once both pipelines
        # had run on the same project.
        clustered_ticket_count = (
            Ticket.objects.filter(project=project, cluster_memberships__cluster__is_noise=False)
            .distinct().count()
        )
        manual_effort_pct = round(max(0, 100 - (clustered_ticket_count / total * 100))) if total else 100
        avg_conf_val = round(sum(c.confidence for c in clusters_qs) / cluster_count) if cluster_count else 0
        low_conf_frac = round((sum(1 for c in clusters_qs if c.confidence < 70) / cluster_count * 100)) if cluster_count else 0
        context.update({
            "manual_effort_pct": manual_effort_pct,
            "avg_confidence": avg_conf_val,
            "false_positive_proxy": low_conf_frac,
        })
    return render(request, "core/how_it_works.html", context)
