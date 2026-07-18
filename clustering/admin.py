from django.contrib import admin

from clustering.models import Cluster, ClusterMember


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "engine", "recurring_count", "confidence", "trend", "is_problem_candidate")
    list_filter = ("project", "engine", "is_problem_candidate", "trend")


admin.site.register(ClusterMember)
