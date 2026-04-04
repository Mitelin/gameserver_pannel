# apps/audit/admin.py
from django.contrib import admin
from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display  = ("server", "timestamp", "event_type", "severity", "user", "message_preview")
    list_filter   = ("server", "severity", "event_type")
    search_fields = ("event_type", "message", "user__username")
    readonly_fields = ("server", "timestamp", "event_type", "severity", "user", "message", "payload_json")
    ordering = ("-timestamp",)

    def message_preview(self, obj):
        return obj.message[:80]
    message_preview.short_description = "Zpráva"

    def has_add_permission(self, request):
        return False  # audit se nezakládá ručně
