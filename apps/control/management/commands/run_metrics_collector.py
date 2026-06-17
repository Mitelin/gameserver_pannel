"""
apps/control/management/commands/run_metrics_collector.py

Sbira metriky, uklada do DB, agreguje, spousti alerts, cleanup.
"""
import signal
import time
import logging
from datetime import timedelta

import psutil
from django.core.management.base import BaseCommand
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from apps.servers.models import Server, ServerStatus
from apps.metrics.models import MetricSample
from apps.metrics.aggregator import aggregate_to_minutes, aggregate_to_hours, run_retention_cleanup
from apps.control.service import _get_backend as _get_process_backend

logger   = logging.getLogger(__name__)
INTERVAL = 12
backend  = _get_process_backend()

_last_minute_agg = {}
_last_hour_agg   = {}
_last_cleanup    = None


class Command(BaseCommand):
    help = "Sbira metriky, uklada do DB, agreguje, cleanup."

    def handle(self, *args, **options):
        self.stdout.write("Metrics collector spusten.")
        self._running = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

        channel_layer = get_channel_layer()
        _init_cpu_counters()

        while self._running:
            now = timezone.now()
            for server in Server.objects.filter(
                is_active=True,
                status__in=[ServerStatus.ONLINE, ServerStatus.STARTING],
            ).select_related("process_state"):
                try:
                    _collect_and_save(server, channel_layer, now)
                    _maybe_aggregate(server, now)
                    _run_metric_alerts(server)
                except Exception as exc:
                    logger.warning("Metrics chyba pro %s: %s", server.slug, exc)

            _maybe_daily_cleanup(now)
            time.sleep(INTERVAL)

        self.stdout.write("Metrics collector ukoncen.")

    def _shutdown(self, signum, frame):
        self.stdout.write(f"Metrics: signal {signum}, ukoncuji...")
        self._running = False


def _collect_and_save(server, channel_layer, now):
    info    = backend.get_process_info(server)

    disk_used = disk_free = None
    try:
        disk = psutil.disk_usage(server.working_directory)
        disk_used, disk_free = disk.used, disk.free
    except Exception:
        pass

    net_rx = net_tx = None
    try:
        net = psutil.net_io_counters()
        net_rx, net_tx = net.bytes_recv, net.bytes_sent
    except Exception:
        pass

    state = server.process_state
    MetricSample.objects.create(
        server             = server,
        timestamp          = now,
        cpu_percent        = info.cpu_percent,
        ram_bytes          = info.rss_bytes,
        thread_count       = info.thread_count,
        disk_used_bytes    = disk_used,
        disk_free_bytes    = disk_free,
        net_rx_bytes       = net_rx,
        net_tx_bytes       = net_tx,
        player_count       = state.last_player_count if state else None,
    )

    state.pid               = info.pid
    state.cpu_percent_last  = info.cpu_percent
    state.rss_bytes_last    = info.rss_bytes
    state.thread_count_last = info.thread_count
    state.last_healthcheck_at = now
    state.save(update_fields=[
        "pid", "cpu_percent_last", "rss_bytes_last",
        "thread_count_last", "last_healthcheck_at",
    ])

    async_to_sync(channel_layer.group_send)(
        f"server.{server.id}.metrics",
        {
            "type":            "metrics.snapshot",
            "server_id":       str(server.id),
            "timestamp":       now.isoformat(),
            "cpu_percent":     info.cpu_percent,
            "ram_bytes":       info.rss_bytes,
            "threads":         info.thread_count,
            "players":         state.last_player_count if state else None,
            "pid":             info.pid,
            "disk_free_bytes": disk_free,
        }
    )
    logger.debug("%s CPU=%.1f%% RAM=%dMB", server.slug,
                 info.cpu_percent or 0, (info.rss_bytes or 0) // 1048576)


def _run_metric_alerts(server):
    """Spusti CPU/RAM threshold alert checks."""
    try:
        from apps.alerts.engine import check_metric_alerts
        state = server.process_state
        check_metric_alerts(
            server,
            cpu_percent=state.cpu_percent_last or 0,
            ram_bytes=state.rss_bytes_last or 0,
        )
    except Exception as exc:
        logger.debug("Alert check chyba: %s", exc)


def _maybe_aggregate(server, now):
    sid = str(server.id)
    last_min = _last_minute_agg.get(sid)
    if last_min is None or (now - last_min).seconds >= 60:
        aggregate_to_minutes(server)
        _last_minute_agg[sid] = now
    last_hr = _last_hour_agg.get(sid)
    if last_hr is None or (now - last_hr).seconds >= 3600:
        aggregate_to_hours(server)
        _last_hour_agg[sid] = now


def _maybe_daily_cleanup(now):
    global _last_cleanup
    if _last_cleanup is None or (now - _last_cleanup) >= timedelta(hours=24):
        result = run_retention_cleanup()
        logger.info("Daily cleanup: %s", result)
        _last_cleanup = now


def _init_cpu_counters():
    try:
        psutil.cpu_percent(interval=None)
        for proc in psutil.process_iter(["cpu_percent"]):
            pass
    except Exception:
        pass
