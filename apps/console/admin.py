# apps/console/admin.py
from django.contrib import admin
from .models import ConsoleLine, CommandHistory


@admin.register(ConsoleLine)
class ConsoleLineAdmin(admin.ModelAdmin):
    list_display  = ("server", "timestamp", "stream_type", "source", "line_preview")
    list_filter   = ("server", "stream_type", "source")
    search_fields = ("line",)
    readonly_fields = ("server", "timestamp", "sequence_number")
    ordering = ("-timestamp",)

    def line_preview(self, obj):
        return obj.line[:80]
    line_preview.short_description = "Řádek"


@admin.register(CommandHistory)
class CommandHistoryAdmin(admin.ModelAdmin):
    list_display  = ("server", "user", "issued_at", "command_preview", "source", "result_status")
    list_filter   = ("server", "source", "result_status")
    search_fields = ("command", "user__username")
    readonly_fields = (
        "server", "user", "issued_at", "accepted_at",
        "dispatched_at", "correlation_id",
    )
    ordering = ("-issued_at",)

    def command_preview(self, obj):
        return obj.command[:60]
    command_preview.short_description = "Příkaz"
