from django.urls import path

from core import views

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("", views.dashboard, name="dashboard"),
    path("projects/", views.projects, name="projects"),
    path("projects/<int:pk>/switch/", views.switch_project, name="switch_project"),
    path("how-it-works/", views.how_it_works, name="how_it_works"),
]
