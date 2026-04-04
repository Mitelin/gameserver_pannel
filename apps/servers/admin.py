# apps/servers/admin.py
from django.contrib import admin
from .models import Server, ServerProcessState, PlayerSession


class ServerProcessStateInline(admin.StackedInline):
    model = ServerProcessState
    extra = 0
    readonly_fields = (
        "pid", "status", "started_at", "stopped_at",
        "last_healthcheck_at", "last_log_line_at", "last_command_at",
        "last_player_count", "cpu_percent_last", "rss_bytes_last",
        "thread_count_last", "consecutive_failures", "last_error",
    )
    can_delete = False


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display  = ("name", "slug", "game_type", "status", "is_active", "host_label", "last_seen_at")
    list_filter   = ("game_type", "status", "is_active")
    search_fields = ("name", "slug", "tmux_session_name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at", "last_seen_at")
    inlines = [ServerProcessStateInline]

    fieldsets = (
        ("Zakladni", {
            "fields": ("id", "name", "slug", "game_type", "is_active", "host_label"),
        }),
        ("Procesni konfigurace", {
            "fields": ("working_directory", "start_command", "stop_command",
                       "tmux_session_name", "log_file_path", "pid_file_path"),
        }),
        ("Timeouty", {
            "fields": ("expected_startup_seconds", "expected_shutdown_seconds"),
        }),
        ("RCON (volitelne)", {
            "classes": ("collapse",),
            "fields": ("rcon_enabled", "rcon_host", "rcon_port", "rcon_password"),
        }),
        ("Notifikace (webhook)", {
            "classes": ("collapse",),
            "fields": ("webhook_url", "webhook_on_crash", "webhook_on_start", "webhook_on_stop"),
        }),
        ("Backup monitoring", {
            "classes": ("collapse",),
            "fields": ("backup_directory", "backup_max_age_hours"),
        }),
        ("Stav (readonly)", {
            "fields": ("status", "last_seen_at", "created_at", "updated_at"),
        }),
    )


@admin.register(PlayerSession)
class PlayerSessionAdmin(admin.ModelAdmin):
    list_display   = ("player_name", "server", "joined_at", "left_at", "duration_human")
    list_filter    = ("server",)
    search_fields  = ("player_name",)
    readonly_fields= ("server", "player_name", "joined_at", "left_at", "duration_seconds")
    ordering       = ("-joined_at",)

    def duration_human(self, obj):
        return obj.duration_human
    duration_human.short_description = "Delka"

    def has_add_permission(self, request):
        return False
