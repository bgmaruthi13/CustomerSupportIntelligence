from django.contrib import admin
from django.shortcuts import redirect

from core.models import Project, SiteSettings


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "domain", "owner", "ticket_count", "created_at")
    search_fields = ("name", "domain")


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ("site_name", "embedding_model_path")

    def has_add_permission(self, request):
        # Singleton — block adding a second row.
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Skip the list page entirely and jump straight to the (only) settings row,
        # creating it on first visit so there's always something to edit.
        obj = SiteSettings.load()
        return redirect("admin:core_sitesettings_change", obj.pk)
