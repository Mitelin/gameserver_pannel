from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


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
