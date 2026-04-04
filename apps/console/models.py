from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class ConsoleLine(models.Model):
    class StreamType(models.TextChoices):
        STDOUT = "stdout", "stdout"
        STDERR = "stderr", "stderr"
        SYSTEM = "system", "Systém"

    class Source(models.TextChoices):
        LOG_TAIL  = "log_tail", "Log tail"
        SYSTEM    = "system",   "Systém"
        SYNTHETIC = "synthetic","Syntetický"

    server          = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="console_lines")
    timestamp       = models.DateTimeField(auto_now_add=True, db_index=True)
    line            = models.TextField()
    stream_type     = models.CharField(max_length=8,  choices=StreamType.choices, default=StreamType.STDOUT)
    source          = models.CharField(max_length=16, choices=Source.choices,     default=Source.LOG_TAIL)
    sequence_number = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["timestamp", "sequence_number"]
        indexes  = [models.Index(fields=["server", "timestamp"])]

    def __str__(self):
        return f"[{self.server.slug}] {self.line[:80]}"


class CommandHistory(models.Model):
    class Source(models.TextChoices):
        WEB_CONSOLE    = "web_console",    "Web konzole"
        ACTION_BUTTON  = "action_button",  "Tlačítko akce"
        SYSTEM         = "system",         "Systém"

    class ResultStatus(models.TextChoices):
        ACCEPTED   = "ACCEPTED",   "Přijato"
        DISPATCHED = "DISPATCHED", "Odesláno"
        OBSERVED   = "OBSERVED",   "Potvrzeno"
        FAILED     = "FAILED",     "Selhalo"
        TIMEOUT    = "TIMEOUT",    "Timeout"

    server         = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="command_history")
    user           = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    issued_at      = models.DateTimeField(auto_now_add=True, db_index=True)
    command        = models.CharField(max_length=1024)
    source         = models.CharField(max_length=16, choices=Source.choices, default=Source.WEB_CONSOLE)
    accepted_at    = models.DateTimeField(null=True, blank=True)
    dispatched_at  = models.DateTimeField(null=True, blank=True)
    result_status  = models.CharField(max_length=16, choices=ResultStatus.choices, default=ResultStatus.ACCEPTED)
    result_message = models.TextField(blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-issued_at"]
