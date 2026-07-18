from django.urls import path

from clustering import views

app_name = "clustering"

urlpatterns = [
    path("", views.clusters_list, name="list"),
    path("reset/<str:engine>/", views.reset_clusters, name="reset_clusters"),
    path("<int:pk>/", views.cluster_detail, name="detail"),
    path("<int:pk>/drill-down/", views.drill_down, name="drill_down"),
    path("explorer/", views.explorer, name="explorer"),
    path("search/", views.search, name="search"),
    path("similar/<int:ticket_id>/", views.similar_tickets, name="similar_tickets"),
    path("categorize/", views.categorize_tickets, name="categorize"),
    path("duplicates/", views.duplicates, name="duplicates"),
    path("traditional-ml/", views.traditional_ml, name="traditional_ml"),
    path("generative-ai/", views.generative_ai, name="generative_ai"),
    path("global/", views.global_clustering, name="global_clustering"),
    path("global/explorer/", views.global_explorer, name="global_explorer"),
    path("global/<int:pk>/", views.global_cluster_detail, name="global_cluster_detail"),
    path("settings/", views.clustering_settings, name="settings"),
]
