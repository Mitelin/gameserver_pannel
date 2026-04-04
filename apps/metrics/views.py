"""
apps/metrics/views.py

JSON API pro grafy.

GET /servers/<slug>/metrics/?range=1h   → raw samply
GET /servers/<slug>/metrics/?range=24h  → minutové agregace
GET /servers/<slug>/metrics/?range=7d   → minutové agregace (downsampled)
GET /servers/<slug>/metrics/?range=30d  → hodinové agregace
GET /servers/<slug>/metrics/?range=90d  → hodinové agregace

Response:
{
  "range": "24h",
  "resolution": "minute",
  "labels": ["12:00", "12:01", ...],
  "datasets": {
    "cpu":     [23.1, 24.5, ...],
    "ram_mb":  [1024, 1030, ...],
    "players": [2, 3, ...],
    "threads": [32, 32, ...]
  }
}
"""
import logging
from datetime import timedelta

from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.servers.models import Server
from apps.metrics.models import MetricSample, MetricMinute, MetricHour

logger = logging.getLogger(__name__)

RANGE_CONFIG = {
    # range_key: (timedelta, resolution, max_points)
    "1h":  (timedelta(hours=1),   "raw",    360),
    "3h":  (timedelta(hours=3),   "raw",    720),
    "6h":  (timedelta(hours=6),   "minute", 360),
    "24h": (timedelta(hours=24),  "minute", 480),
    "7d":  (timedelta(days=7),    "minute", 504),   # každých 20 minut
    "30d": (timedelta(days=30),   "hour",   720),
    "90d": (timedelta(days=90),   "hour",   720),
}


class MetricsAPIView(LoginRequiredMixin, View):
    raise_exception = True

    def get(self, request, slug):
        server   = get_object_or_404(Server, slug=slug, is_active=True)
        range_key = request.GET.get("range", "24h")

        if range_key not in RANGE_CONFIG:
            return JsonResponse({"error": f"Neplatný range. Povolené: {list(RANGE_CONFIG)}"}, status=400)

        delta, resolution, max_points = RANGE_CONFIG[range_key]
        since = timezone.now() - delta

        if resolution == "raw":
            data = _fetch_raw(server, since, max_points)
        elif resolution == "minute":
            data = _fetch_minutes(server, since, max_points)
        else:
            data = _fetch_hours(server, since, max_points)

        return JsonResponse({
            "range":      range_key,
            "resolution": resolution,
            **data,
        })


def _fetch_raw(server, since, max_points):
    qs = (
        MetricSample.objects
        .filter(server=server, timestamp__gte=since)
        .order_by("timestamp")
        .values("timestamp", "cpu_percent", "ram_bytes", "player_count", "thread_count")
    )
    samples = _downsample(list(qs), max_points)

    labels  = [_fmt_time(s["timestamp"]) for s in samples]
    return {
        "labels":   labels,
        "datasets": {
            "cpu":     [_r(s["cpu_percent"])             for s in samples],
            "ram_mb":  [_bytes_to_mb(s["ram_bytes"])     for s in samples],
            "players": [s["player_count"]                for s in samples],
            "threads": [s["thread_count"]                for s in samples],
        }
    }


def _fetch_minutes(server, since, max_points):
    qs = (
        MetricMinute.objects
        .filter(server=server, timestamp__gte=since)
        .order_by("timestamp")
        .values("timestamp", "cpu_avg", "cpu_max", "ram_avg", "ram_max", "player_avg", "player_max", "thread_avg")
    )
    rows = _downsample(list(qs), max_points)

    labels = [_fmt_time(r["timestamp"]) for r in rows]
    return {
        "labels":   labels,
        "datasets": {
            "cpu":       [_r(r["cpu_avg"])           for r in rows],
            "cpu_max":   [_r(r["cpu_max"])           for r in rows],
            "ram_mb":    [_bytes_to_mb(r["ram_avg"]) for r in rows],
            "ram_mb_max":[_bytes_to_mb(r["ram_max"]) for r in rows],
            "players":   [_r(r["player_avg"])        for r in rows],
            "threads":   [_r(r["thread_avg"])        for r in rows],
        }
    }


def _fetch_hours(server, since, max_points):
    qs = (
        MetricHour.objects
        .filter(server=server, timestamp__gte=since)
        .order_by("timestamp")
        .values("timestamp", "cpu_avg", "cpu_max", "ram_avg", "ram_max", "player_avg", "player_max")
    )
    rows = _downsample(list(qs), max_points)

    labels = [_fmt_datetime(r["timestamp"]) for r in rows]
    return {
        "labels":   labels,
        "datasets": {
            "cpu":       [_r(r["cpu_avg"])           for r in rows],
            "cpu_max":   [_r(r["cpu_max"])           for r in rows],
            "ram_mb":    [_bytes_to_mb(r["ram_avg"]) for r in rows],
            "ram_mb_max":[_bytes_to_mb(r["ram_max"]) for r in rows],
            "players":   [_r(r["player_avg"])        for r in rows],
        }
    }


# ─── helpers ────────────────────────────────────────────────

def _downsample(rows: list, max_points: int) -> list:
    """Rovnoměrně prořeď seznam na max_points prvků."""
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    return [rows[int(i * step)] for i in range(max_points)]


def _fmt_time(ts) -> str:
    if ts is None:
        return ""
    return ts.strftime("%H:%M")


def _fmt_datetime(ts) -> str:
    if ts is None:
        return ""
    return ts.strftime("%d.%m %H:%M")


def _bytes_to_mb(b) -> float | None:
    if b is None:
        return None
    return round(b / 1048576, 1)


def _r(v) -> float | None:
    if v is None:
        return None
    return round(v, 2)
