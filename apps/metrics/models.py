"""
apps/metrics/models.py

Tři úrovně granularity:
  MetricSample    – raw data každých ~12s, retention 7 dní
  MetricMinute    – agregace po minutách, retention 60 dní
  MetricHour      – agregace po hodinách, retention navždy (nebo 2 roky)

Při dotazu na grafy se vybírá správná tabulka podle časového rozsahu:
  < 3h  → MetricSample (raw)
  < 7d  → MetricMinute
  else  → MetricHour
"""
from django.db import models


class MetricSample(models.Model):
    """Raw snapshot každých 10–15 sekund."""
    server          = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="metric_samples")
    timestamp       = models.DateTimeField(db_index=True)

    # CPU
    cpu_percent     = models.FloatField(null=True)

    # RAM procesu
    ram_bytes       = models.BigIntegerField(null=True)

    # Celkový systém
    system_cpu_percent = models.FloatField(null=True)
    system_ram_bytes   = models.BigIntegerField(null=True)

    # Proces
    thread_count    = models.IntegerField(null=True)
    open_files      = models.IntegerField(null=True)

    # Disk (pracovní adresář)
    disk_used_bytes = models.BigIntegerField(null=True)
    disk_free_bytes = models.BigIntegerField(null=True)

    # Síť (delta od předchozího sample)
    net_rx_bytes    = models.BigIntegerField(null=True)
    net_tx_bytes    = models.BigIntegerField(null=True)

    # Game-specific
    player_count    = models.IntegerField(null=True)
    max_players     = models.IntegerField(null=True)
    tps             = models.FloatField(null=True)   # pro Minecraft

    class Meta:
        ordering = ["timestamp"]
        indexes  = [models.Index(fields=["server", "timestamp"])]


class MetricMinute(models.Model):
    """Agregace po 1 minutě – průměry za všechny samply v minutě."""
    server          = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="metric_minutes")
    timestamp       = models.DateTimeField(db_index=True)  # vždy začátek minuty (truncated)

    cpu_avg         = models.FloatField(null=True)
    cpu_max         = models.FloatField(null=True)
    ram_avg         = models.BigIntegerField(null=True)
    ram_max         = models.BigIntegerField(null=True)
    player_avg      = models.FloatField(null=True)
    player_max      = models.IntegerField(null=True)
    thread_avg      = models.FloatField(null=True)
    sample_count    = models.IntegerField(default=0)

    class Meta:
        ordering = ["timestamp"]
        unique_together = [["server", "timestamp"]]
        indexes  = [models.Index(fields=["server", "timestamp"])]


class MetricHour(models.Model):
    """Agregace po 1 hodině – průměry za všechny minuty v hodině."""
    server          = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="metric_hours")
    timestamp       = models.DateTimeField(db_index=True)  # vždy začátek hodiny

    cpu_avg         = models.FloatField(null=True)
    cpu_max         = models.FloatField(null=True)
    ram_avg         = models.BigIntegerField(null=True)
    ram_max         = models.BigIntegerField(null=True)
    player_avg      = models.FloatField(null=True)
    player_max      = models.IntegerField(null=True)
    sample_count    = models.IntegerField(default=0)

    class Meta:
        ordering = ["timestamp"]
        unique_together = [["server", "timestamp"]]
        indexes  = [models.Index(fields=["server", "timestamp"])]
