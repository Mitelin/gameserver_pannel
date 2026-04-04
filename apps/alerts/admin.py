# apps/alerts/admin.py
from django.contrib import admin
from .models import AlertRule, AlertFire


class AlertFireInline(admin.TabularInline):
    model      = AlertFire
    extra      = 0
    readonly_fields = ("fired_at", "details", "sent_ok")
    can_delete = False
    max_num    = 10
    ordering   = ("-fired_at",)


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display  = ("name", "server", "condition_type", "is_active", "cooldown_minutes", "channel")
    list_filter   = ("server", "condition_type", "is_active", "channel")
    search_fields = ("name", "server__name")
    inlines       = [AlertFireInline]

    fieldsets = (
        ("Základní", {
            "fields": ("server", "name", "is_active"),
        }),
        ("Podmínka", {
            "fields": ("condition_type", "status_values", "threshold_value",
                       "duration_minutes", "log_pattern"),
        }),
        ("Doručení", {
            "fields": ("channel", "webhook_url", "message_template", "cooldown_minutes"),
        }),
    )


@admin.register(AlertFire)
class AlertFireAdmin(admin.ModelAdmin):
    list_display  = ("rule", "fired_at", "sent_ok", "details_preview")
    list_filter   = ("sent_ok", "rule__server")
    readonly_fields = ("rule", "fired_at", "details", "sent_ok")
    ordering      = ("-fired_at",)

    def details_preview(self, obj):
        return obj.details[:80]
    details_preview.short_description = "Detail"

    def has_add_permission(self, request):
        return False
