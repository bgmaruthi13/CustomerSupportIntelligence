from core.models import Project, SiteSettings


def site_settings_context(request):
    """Injects site_settings into every template, including anonymous pages like login."""
    return {"site_settings": SiteSettings.load()}


def tenant_context(request):
    """Injects current_project and user_projects into every template."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}

    projects = Project.objects.filter(owner=request.user).order_by("-created_at")
    current_project = None

    project_id = request.session.get("current_project_id")
    if project_id:
        current_project = projects.filter(id=project_id).first()

    if not current_project:
        current_project = projects.first()
        if current_project:
            request.session["current_project_id"] = current_project.id

    return {
        "current_project": current_project,
        "user_projects": projects,
    }
