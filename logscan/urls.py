from django.urls import path

from logscan import views

app_name = "logscan"

urlpatterns = [
    path("sources/", views.sources, name="sources"),
    path("sources/<int:pk>/delete/", views.delete_source, name="delete_source"),
    path("sources/<int:pk>/scan/", views.scan_now, name="scan_now"),
    path("jobs/", views.jobs_list, name="jobs_list"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/status/", views.job_status_json, name="job_status_json"),
    path("findings/", views.findings_report, name="findings_report"),
]
