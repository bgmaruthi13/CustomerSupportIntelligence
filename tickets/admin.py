from django.contrib import admin

from tickets.models import Ticket, UploadBatch


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("external_id", "title", "project", "country", "application", "created_at")
    search_fields = ("external_id", "title")
    list_filter = ("project", "country", "application", "status")


@admin.register(UploadBatch)
class UploadBatchAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "project", "status", "total_rows", "success_rows", "error_rows", "created_at")
    list_filter = ("project", "status")
