"""
apps/setup/models.py

SystemSettings  – singleton konfigurace panelu
BootstrapState  – stav prvního spuštění (setup wizard)
"""
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class SystemSettings(models.Model):
    """
    Singleton konfigurace panelu. Přistupuj vždy přes SystemSettings.get().
    """
    # ── Branding ──────────────────────────────────────────────────────────
    panel_name      = models.CharField(max_length=128, default="Gameserver Panel")
    public_base_url = models.CharField(
        max_length=256, blank=True,
        help_text="Veřejná URL panelu, např. https://panel.example.com",
    )

    # ── Lokalizace ────────────────────────────────────────────────────────
    timezone         = models.CharField(max_length=64, default="Europe/Prague")
    default_language = models.CharField(max_length=8,  default="cs")

    # ── Výchozí timeouty serverů ──────────────────────────────────────────
    default_startup_timeout  = models.IntegerField(
        default=60, help_text="Výchozí timeout startu serveru (sekundy)",
    )
    default_shutdown_timeout = models.IntegerField(
        default=30, help_text="Výchozí timeout vypnutí serveru (sekundy)",
    )

    # ── Retence dat ───────────────────────────────────────────────────────
    raw_metrics_retention_days    = models.IntegerField(
        default=7,  help_text="Retence raw metrik (dny)",
    )
    minute_metrics_retention_days = models.IntegerField(
        default=60, help_text="Retence minutových metrik (dny)",
    )
    console_retention_days        = models.IntegerField(
        default=14, help_text="Retence konzole logů (dny)",
    )

    # ── Runtime polling ───────────────────────────────────────────────────
    runtime_poll_console_ms       = models.IntegerField(
        default=500, help_text="Interval tailu konzole (ms)",
    )
    runtime_poll_metrics_seconds  = models.IntegerField(
        default=12,  help_text="Interval sběru metrik (sekundy)",
    )
    runtime_poll_watchdog_seconds = models.IntegerField(
        default=15,  help_text="Interval watchdogu (sekundy)",
    )

    # ── Alerting ──────────────────────────────────────────────────────────
    alert_cooldown_minutes = models.IntegerField(
        default=15, help_text="Minimální pauza mezi stejnými alerty (minuty)",
    )

    class Meta:
        verbose_name        = "System Settings"
        verbose_name_plural = "System Settings"

    @classmethod
    def get(cls):
        """Vrátí singleton instanci, nebo ji vytvoří s výchozími hodnotami."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"SystemSettings ({self.panel_name})"


class BootstrapState(models.Model):
    """Eviduje, zda byl dokončen setup wizard."""
    is_completed   = models.BooleanField(default=False)
    completed_at   = models.DateTimeField(null=True, blank=True)
    completed_by   = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
    )
    schema_version = models.CharField(max_length=32, default="1.0")

    class Meta:
        verbose_name = "Bootstrap State"

    @classmethod
    def is_done(cls) -> bool:
        """Rychlý check – nepadá při problému s DB (migrace ještě neproběhly)."""
        try:
            return cls.objects.filter(is_completed=True).exists()
        except Exception:
            return False

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Bootstrap " + ("dokončen" if self.is_completed else "čeká")
