from django.urls import path

from logscan import views

app_name = "logscan"

urlpatterns = [
    path("sources/", views.sources, name="sources"),
    path("sources/<int:pk>/delete/", views.delete_source, name="delete_source"),
    path("sources/<int:pk>/scan/", views.scan_now, name="scan_now"),
    path("sources/<int:pk>/find-patterns/", views.find_patterns, name="find_patterns"),
    path("sources/<int:pk>/patterns/", views.patterns, name="patterns"),
    path("sources/<int:pk>/patterns/download/", views.patterns_download, name="patterns_download"),
    path("jobs/", views.jobs_list, name="jobs_list"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/status/", views.job_status_json, name="job_status_json"),
    path("findings/", views.findings_report, name="findings_report"),
    path("findings/download/", views.findings_download, name="findings_download"),
    path("correlations/", views.correlations, name="correlations"),
    path("correlations/run/", views.run_correlation, name="run_correlation"),
    path("correlations/download/", views.correlations_download, name="correlations_download"),
]
