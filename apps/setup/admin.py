from django.contrib import admin
from .models import SystemSettings, BootstrapState


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ("panel_name", "timezone", "default_language")


@admin.register(BootstrapState)
class BootstrapStateAdmin(admin.ModelAdmin):
    list_display = ("is_completed", "completed_at", "completed_by", "schema_version")
    readonly_fields = ("completed_at", "completed_by")
