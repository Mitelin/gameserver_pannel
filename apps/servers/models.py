"""
apps/servers/models.py

Všechny server-related modely v jednom souboru:
  - ServerStatus, GameType (enums)
  - Server (hlavní konfigurace)
  - ServerProcessState (runtime stav)
  - PlayerSession (join/leave historie) — fáze 5
"""
from django.db import models
import uuid


class ServerStatus(models.TextChoices):
    OFFLINE  = "OFFLINE",  "Offline"
    STARTING = "STARTING", "Startuje"
    ONLINE   = "ONLINE",   "Online"
    STOPPING = "STOPPING", "Zastavuje se"
    CRASHED  = "CRASHED",  "Crashed"
    UNKNOWN  = "UNKNOWN",  "Neznámý"


class GameType(models.TextChoices):
    MINECRAFT_JAVA    = "minecraft_java",    "Minecraft Java"
    MINECRAFT_BEDROCK = "minecraft_bedrock", "Minecraft Bedrock"
    TERRARIA          = "terraria",          "Terraria"
    FACTORIO          = "factorio",          "Factorio"
    OTHER             = "other",             "Ostatní"


class Server(models.Model):
    id                        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name                      = models.CharField(max_length=128)
    slug                      = models.SlugField(unique=True)
    game_type                 = models.CharField(max_length=32, choices=GameType.choices, default=GameType.OTHER)
    is_active                 = models.BooleanField(default=True)

    # Procesní konfigurace
    working_directory         = models.CharField(max_length=512)
    start_command             = models.CharField(max_length=512, blank=True, default="")
    stop_command              = models.CharField(max_length=512, blank=True)
    tmux_session_name         = models.CharField(max_length=64, unique=True)
    log_file_path             = models.CharField(max_length=512, blank=True, default="")
    pid_file_path             = models.CharField(max_length=512, blank=True)

    # Multi-host (budoucí rozšíření)
    host_label                = models.CharField(max_length=64, default="localhost")

    # RCON
    rcon_enabled              = models.BooleanField(default=False)
    rcon_host                 = models.CharField(max_length=128, blank=True)
    rcon_port                 = models.IntegerField(null=True, blank=True)
    rcon_password             = models.CharField(max_length=128, blank=True)

    # Timeouty stavového automatu
    expected_startup_seconds  = models.IntegerField(default=60)
    expected_shutdown_seconds = models.IntegerField(default=30)

    # Stav
    status                    = models.CharField(
        max_length=16, choices=ServerStatus.choices, default=ServerStatus.OFFLINE
    )
    last_seen_at              = models.DateTimeField(null=True, blank=True)
    created_at                = models.DateTimeField(auto_now_add=True)
    updated_at                = models.DateTimeField(auto_now=True)

    # Webhook notifikace
    webhook_url               = models.URLField(blank=True)
    webhook_on_crash          = models.BooleanField(default=True)
    webhook_on_start          = models.BooleanField(default=False)
    webhook_on_stop           = models.BooleanField(default=False)

    # Backup (fáze 5 + 11)
    backup_directory          = models.CharField(max_length=512, blank=True)
    backup_max_age_hours      = models.IntegerField(default=24)
    backup_keep_count         = models.IntegerField(default=7, help_text="Počet záloh k uchování (rotace)")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.slug})"

    @property
    def is_controllable(self):
        return self.status not in (ServerStatus.STARTING, ServerStatus.STOPPING)


class ServerProcessState(models.Model):
    server               = models.OneToOneField(Server, on_delete=models.CASCADE, related_name="process_state")
    pid                  = models.IntegerField(null=True, blank=True)
    status               = models.CharField(max_length=16, choices=ServerStatus.choices, default=ServerStatus.OFFLINE)

    started_at           = models.DateTimeField(null=True, blank=True)
    stopped_at           = models.DateTimeField(null=True, blank=True)
    last_healthcheck_at  = models.DateTimeField(null=True, blank=True)
    last_log_line_at     = models.DateTimeField(null=True, blank=True)
    last_command_at      = models.DateTimeField(null=True, blank=True)

    last_player_count    = models.IntegerField(default=0)
    cpu_percent_last     = models.FloatField(null=True, blank=True)
    rss_bytes_last       = models.BigIntegerField(null=True, blank=True)
    thread_count_last    = models.IntegerField(null=True, blank=True)
    last_error           = models.TextField(blank=True)
    consecutive_failures = models.IntegerField(default=0)

    def __str__(self):
        return f"ProcessState({self.server.slug}, {self.status})"


class StartProfile(models.Model):
    """
    JVM start profil pro Minecraft server.
    Sestaví start_command z komponent místo ručního psaní.
    """
    server       = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="start_profiles")
    name         = models.CharField(max_length=64)
    is_active    = models.BooleanField(default=False)

    # JVM parametry
    jar_file     = models.CharField(max_length=256, default="server.jar",
                                    help_text="Název jar souboru v working_directory")
    heap_min_mb  = models.IntegerField(default=512,  help_text="Xms v MB")
    heap_max_mb  = models.IntegerField(default=2048, help_text="Xmx v MB")
    jvm_flags    = models.TextField(blank=True,
                                    help_text="Další JVM flagy, každý na novém řádku")
    extra_args   = models.TextField(blank=True,
                                    help_text="Argumenty za jar souborem (např. --nogui)")

    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("server", "name")]

    def __str__(self):
        return f"{self.server.slug} / {self.name}"

    def build_command(self) -> str:
        """Sestaví start příkaz z komponent."""
        flags = " ".join(f.strip() for f in self.jvm_flags.splitlines() if f.strip())
        extra = " ".join(e.strip() for e in self.extra_args.splitlines() if e.strip())
        parts = ["java", f"-Xms{self.heap_min_mb}m", f"-Xmx{self.heap_max_mb}m"]
        if flags:
            parts.append(flags)
        parts += ["-jar", self.jar_file]
        if extra:
            parts.append(extra)
        return " ".join(parts)

    def activate(self):
        """Nastaví tento profil jako aktivní. Backend použije build_command() při startu."""
        StartProfile.objects.filter(server=self.server, is_active=True).update(is_active=False)
        self.is_active = True
        self.save(update_fields=["is_active"])


class ScheduledRestart(models.Model):
    """
    Plánovaný restart serveru dle cron výrazu.
    Příklad: '0 4 * * *' = každý den ve 4:00.
    """
    server            = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="scheduled_restarts")
    label             = models.CharField(max_length=64, blank=True, help_text="Volitelný popis, např. 'Noční restart'")
    cron_expression   = models.CharField(max_length=64, help_text="5-polní cron výraz, např. '0 4 * * *'")
    is_active         = models.BooleanField(default=True)
    warn_minutes      = models.IntegerField(default=5, help_text="Kolik minut předem varovat hráče (0 = bez varování)")
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["cron_expression"]

    def __str__(self):
        return f"{self.server.slug} / {self.label or self.cron_expression}"

    def get_label(self):
        return self.label or self.cron_expression


class PlayerSession(models.Model):
    """Historické záznamy join/leave hráčů."""
    server           = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="player_sessions")
    player_name      = models.CharField(max_length=64, db_index=True)
    joined_at        = models.DateTimeField(db_index=True)
    left_at          = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-joined_at"]
        indexes  = [
            models.Index(fields=["server", "joined_at"]),
            models.Index(fields=["player_name"]),
        ]

    def __str__(self):
        return f"{self.player_name} @ {self.server.slug}"

    @property
    def duration_human(self) -> str:
        if not self.duration_seconds:
            return "–"
        h, m = divmod(self.duration_seconds // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"
