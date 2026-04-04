"""
apps/alerts/models.py

Alerting systém – konfigurovatelná upozornění na různé events.

Alert má:
  - podmínku (event_type pattern nebo metric threshold)
  - kanál (webhook, email v budoucnu)
  - cooldown (aby neposílal spam)

Příklady alertů:
  - server.status == CRASHED → webhook
  - cpu_percent > 90% po dobu 5 minut → webhook
  - žádný hráč 60 minut → webhook (volitelné)
"""
from django.db import models
import uuid


class AlertChannel(models.TextChoices):
    WEBHOOK = "webhook", "Webhook (Discord/Slack/Teams)"
    # EMAIL = "email", "Email"  # budoucí fáze


class AlertConditionType(models.TextChoices):
    STATUS_CHANGE   = "status_change",   "Změna stavu serveru"
    CPU_THRESHOLD   = "cpu_threshold",   "CPU přes limit"
    RAM_THRESHOLD   = "ram_threshold",   "RAM přes limit"
    NO_PLAYERS      = "no_players",      "Žádní hráči"
    LOG_PATTERN     = "log_pattern",     "Pattern v logu"


class AlertRule(models.Model):
    """Jedno pravidlo alertu pro jeden server."""
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    server      = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="alert_rules")
    name        = models.CharField(max_length=128)
    is_active   = models.BooleanField(default=True)

    # Podmínka
    condition_type  = models.CharField(max_length=32, choices=AlertConditionType.choices)
    # Pro status_change: "CRASHED,UNKNOWN" (čárkou oddělené statusy)
    status_values   = models.CharField(max_length=128, blank=True)
    # Pro cpu/ram threshold: číslo v %/MB
    threshold_value = models.FloatField(null=True, blank=True)
    # Pro no_players: počet minut
    duration_minutes = models.IntegerField(null=True, blank=True)
    # Pro log_pattern: regex
    log_pattern     = models.CharField(max_length=256, blank=True)

    # Kanál
    channel     = models.CharField(max_length=16, choices=AlertChannel.choices, default=AlertChannel.WEBHOOK)
    webhook_url = models.URLField()
    # Šablona zprávy – podporuje {server_name}, {status}, {value}
    message_template = models.TextField(
        default="🚨 [{server_name}] Alert: {condition} – {details}",
        help_text="Dostupné proměnné: {server_name}, {status}, {value}, {condition}"
    )

    # Cooldown – po odeslání alertu neodesílat znovu po dobu X minut
    cooldown_minutes = models.IntegerField(default=15)

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["server", "name"]

    def __str__(self):
        return f"{self.server.slug} – {self.name}"


class AlertFire(models.Model):
    """Zaznamená kdy byl alert odeslán (pro cooldown kontrolu)."""
    rule       = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="fires")
    fired_at   = models.DateTimeField(auto_now_add=True)
    details    = models.TextField(blank=True)
    sent_ok    = models.BooleanField(default=True)

    class Meta:
        ordering = ["-fired_at"]
