"""
apps/metrics/aggregator.py

Agregační engine:
  - raw samples → MetricMinute (spouštěno každou minutu)
  - MetricMinute → MetricHour  (spouštěno každou hodinu)
  - retention cleanup          (spouštěno jednou denně)

Navrženo tak, aby šlo volat jak z management commandu,
tak později z Celery tasku.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Avg, Max, Count, Min
from django.utils import timezone

from apps.servers.models import Server
from apps.metrics.models import MetricSample, MetricMinute, MetricHour

logger = logging.getLogger(__name__)

# Retention limity
RAW_RETENTION_DAYS    = 7
MINUTE_RETENTION_DAYS = 60


def aggregate_to_minutes(server: Server, since=None):
    """
    Vezme raw samply a agreguje je do MetricMinute záznamy.
    Zpracovává jen minuty, pro které ještě MetricMinute neexistuje.
    """
    if since is None:
        # Od poslední existující minutové agregace
        last = MetricMinute.objects.filter(server=server).order_by("-timestamp").first()
        since = last.timestamp if last else (timezone.now() - timedelta(hours=2))

    # Zaokrouhlit na celou minutu dolů
    since_trunc = since.replace(second=0, microsecond=0)
    now_trunc   = timezone.now().replace(second=0, microsecond=0)

    # Samply od since do teď (bez aktuální minuty – ta ještě není kompletní)
    samples = (
        MetricSample.objects
        .filter(server=server, timestamp__gte=since_trunc, timestamp__lt=now_trunc)
        .order_by("timestamp")
    )

    if not samples.exists():
        return 0

    # Seskupit po minutách ručně (DB-agnostické)
    buckets: dict = {}
    for s in samples:
        bucket_key = s.timestamp.replace(second=0, microsecond=0)
        if bucket_key not in buckets:
            buckets[bucket_key] = []
        buckets[bucket_key].append(s)

    created = 0
    with transaction.atomic():
        for minute_ts, bucket in buckets.items():
            cpu_vals  = [s.cpu_percent  for s in bucket if s.cpu_percent  is not None]
            ram_vals  = [s.ram_bytes    for s in bucket if s.ram_bytes    is not None]
            play_vals = [s.player_count for s in bucket if s.player_count is not None]
            thr_vals  = [s.thread_count for s in bucket if s.thread_count is not None]

            MetricMinute.objects.update_or_create(
                server=server,
                timestamp=minute_ts,
                defaults={
                    "cpu_avg":      _avg(cpu_vals),
                    "cpu_max":      max(cpu_vals)  if cpu_vals  else None,
                    "ram_avg":      _avg(ram_vals),
                    "ram_max":      max(ram_vals)  if ram_vals  else None,
                    "player_avg":   _avg(play_vals),
                    "player_max":   max(play_vals) if play_vals else None,
                    "thread_avg":   _avg(thr_vals),
                    "sample_count": len(bucket),
                }
            )
            created += 1

    logger.debug("aggregate_to_minutes: %s → %d bucket(s)", server.slug, created)
    return created


def aggregate_to_hours(server: Server, since=None):
    """
    Vezme MetricMinute záznamy a agreguje do MetricHour.
    """
    if since is None:
        last = MetricHour.objects.filter(server=server).order_by("-timestamp").first()
        since = last.timestamp if last else (timezone.now() - timedelta(days=3))

    since_trunc = since.replace(minute=0, second=0, microsecond=0)
    now_trunc   = timezone.now().replace(minute=0, second=0, microsecond=0)

    minutes = (
        MetricMinute.objects
        .filter(server=server, timestamp__gte=since_trunc, timestamp__lt=now_trunc)
        .order_by("timestamp")
    )

    if not minutes.exists():
        return 0

    buckets: dict = {}
    for m in minutes:
        bucket_key = m.timestamp.replace(minute=0, second=0, microsecond=0)
        if bucket_key not in buckets:
            buckets[bucket_key] = []
        buckets[bucket_key].append(m)

    created = 0
    with transaction.atomic():
        for hour_ts, bucket in buckets.items():
            cpu_vals  = [m.cpu_avg    for m in bucket if m.cpu_avg    is not None]
            cpu_maxes = [m.cpu_max    for m in bucket if m.cpu_max    is not None]
            ram_vals  = [m.ram_avg    for m in bucket if m.ram_avg    is not None]
            ram_maxes = [m.ram_max    for m in bucket if m.ram_max    is not None]
            play_vals = [m.player_avg for m in bucket if m.player_avg is not None]
            play_maxes= [m.player_max for m in bucket if m.player_max is not None]

            MetricHour.objects.update_or_create(
                server=server,
                timestamp=hour_ts,
                defaults={
                    "cpu_avg":      _avg(cpu_vals),
                    "cpu_max":      max(cpu_maxes) if cpu_maxes else None,
                    "ram_avg":      _avg(ram_vals),
                    "ram_max":      max(ram_maxes) if ram_maxes else None,
                    "player_avg":   _avg(play_vals),
                    "player_max":   max(play_maxes) if play_maxes else None,
                    "sample_count": len(bucket),
                }
            )
            created += 1

    logger.debug("aggregate_to_hours: %s → %d bucket(s)", server.slug, created)
    return created


def run_retention_cleanup():
    """
    Smaže stará data přes všechny tabulky.
    Bezpečné volat denně.
    """
    now = timezone.now()

    raw_cutoff    = now - timedelta(days=RAW_RETENTION_DAYS)
    minute_cutoff = now - timedelta(days=MINUTE_RETENTION_DAYS)
    deleted_raw, _    = MetricSample.objects.filter(timestamp__lt=raw_cutoff).delete()
    deleted_min, _    = MetricMinute.objects.filter(timestamp__lt=minute_cutoff).delete()

    logger.info(
        "Retention cleanup: raw=%d minute=%d",
        deleted_raw, deleted_min,
    )
    return {
        "deleted_raw":     deleted_raw,
        "deleted_minutes": deleted_min,
    }


def _avg(values: list):
    if not values:
        return None
    return sum(values) / len(values)
