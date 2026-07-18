from django.urls import path

from tickets import views

app_name = "tickets"

urlpatterns = [
    path("upload/", views.upload, name="upload"),
    path("upload/<int:batch_id>/mapping/", views.mapping, name="mapping"),
    path("upload/<int:batch_id>/result/", views.upload_result, name="upload_result"),
    path("upload/<int:batch_id>/delete/", views.delete_batch, name="delete_batch"),
    path("delete-all/", views.delete_all_data, name="delete_all_data"),
    path("analysis/", views.eda, name="eda"),
    path("sensitive-data/", views.sensitive_data_report, name="sensitive_data_report"),
]
