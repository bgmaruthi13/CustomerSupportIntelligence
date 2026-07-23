from django.contrib import admin

from logscan.models import LogPatternCluster, LogPIIFinding, LogScanJob, LogSource, LogSourceFile


@admin.register(LogSource)
class LogSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "source_type", "trigger_mode", "is_active", "last_scanned_offset", "created_at")
    list_filter = ("project", "source_type", "trigger_mode", "is_active")


@admin.register(LogScanJob)
class LogScanJobAdmin(admin.ModelAdmin):
    list_display = ("source", "status", "triggered_by", "bytes_scanned", "findings_count", "started_at", "finished_at")
    list_filter = ("status", "triggered_by")


@admin.register(LogSourceFile)
class LogSourceFileAdmin(admin.ModelAdmin):
    list_display = ("source", "relative_path", "last_scanned_offset", "last_seen_at")
    list_filter = ("source",)


@admin.register(LogPatternCluster)
class LogPatternClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "source", "file_path", "engine", "recurring_count", "confidence", "is_noise", "created_at")
    list_filter = ("source", "engine", "is_noise")


admin.site.register(LogPIIFinding)
