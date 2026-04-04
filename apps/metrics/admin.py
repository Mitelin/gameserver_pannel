# apps/metrics/admin.py
from django.contrib import admin
from .models import MetricSample, MetricMinute, MetricHour


@admin.register(MetricSample)
class MetricSampleAdmin(admin.ModelAdmin):
    list_display  = ("server", "timestamp", "cpu_percent", "ram_mb", "player_count", "thread_count")
    list_filter   = ("server",)
    readonly_fields = [f.name for f in MetricSample._meta.fields]
    ordering      = ("-timestamp",)

    def ram_mb(self, obj):
        return f"{obj.ram_bytes // 1048576} MB" if obj.ram_bytes else "–"
    ram_mb.short_description = "RAM"

    def has_add_permission(self, request):
        return False


@admin.register(MetricMinute)
class MetricMinuteAdmin(admin.ModelAdmin):
    list_display = ("server", "timestamp", "cpu_avg", "ram_mb_avg", "player_avg", "sample_count")
    list_filter  = ("server",)
    ordering     = ("-timestamp",)

    def ram_mb_avg(self, obj):
        return f"{obj.ram_avg // 1048576} MB" if obj.ram_avg else "–"
    ram_mb_avg.short_description = "RAM avg"

    def has_add_permission(self, request):
        return False


@admin.register(MetricHour)
class MetricHourAdmin(admin.ModelAdmin):
    list_display = ("server", "timestamp", "cpu_avg", "cpu_max", "player_avg", "sample_count")
    list_filter  = ("server",)
    ordering     = ("-timestamp",)

    def has_add_permission(self, request):
        return False
