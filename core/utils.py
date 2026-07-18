import json

from django.shortcuts import get_object_or_404

from core.models import Project


def dumps_for_script(obj):
    """json.dumps for a value that will be embedded in a template via `{{ ...|safe }}`
    inside a <script> block. Plain json.dumps leaves "<", ">", "&" untouched, so a
    user-supplied string containing "</script>" (e.g. a ticket title from an upload)
    would close the block early and let the rest run as HTML/script — escaping those
    three characters to \\uXXXX keeps the JSON valid while making that impossible."""
    return (
        json.dumps(obj)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def get_current_project(request):
    """Resolves the active tenant Project for this request, honoring the session switch."""
    project_id = request.session.get("current_project_id")
    qs = Project.objects.filter(owner=request.user)
    if project_id:
        project = qs.filter(id=project_id).first()
        if project:
            return project
    project = qs.order_by("-created_at").first()
    if project:
        request.session["current_project_id"] = project.id
    return project


def require_project_or_404(request, project_id):
    return get_object_or_404(Project, id=project_id, owner=request.user)


def apply_sort(request, queryset, field_map, default_field, default_dir="desc", param_prefix=""):
    """Orders `queryset` per ?sort=<key>&dir=asc|desc, where `field_map` maps a
    URL-safe sort key (what the template's sort_header tag writes) to the actual
    model field(s) passed to .order_by(). Falls back to `default_field`/`default_dir`
    for a missing or unrecognized `sort` key, so a stale/forged querystring value
    can't raise a FieldError — it just resets to the page's normal ordering.

    `param_prefix` namespaces the querystring keys (e.g. "members" ->
    ?members_sort=&members_dir=) for pages with more than one independently
    sortable table — without it, two tables sharing the plain sort/dir keys
    would clobber each other's selection. Must match the same `param_prefix`
    passed to `{% sort_header %}` in the template."""
    sort_param = f"{param_prefix}_sort" if param_prefix else "sort"
    dir_param = f"{param_prefix}_dir" if param_prefix else "dir"
    sort_key = request.GET.get(sort_param)
    direction = request.GET.get(dir_param, default_dir)
    if sort_key not in field_map:
        sort_key = default_field
        direction = default_dir
    field = field_map[sort_key]
    order = field if direction == "asc" else f"-{field}"
    return queryset.order_by(order)
