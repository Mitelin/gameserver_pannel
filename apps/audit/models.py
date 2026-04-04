from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class AuditEvent(models.Model):
    class Severity(models.TextChoices):
        INFO    = "info",    "Info"
        WARNING = "warning", "Varování"
        ERROR   = "error",   "Chyba"

    server      = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="audit_events")
    timestamp   = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type  = models.CharField(max_length=64, db_index=True)
    severity    = models.CharField(max_length=8, choices=Severity.choices, default=Severity.INFO)
    user        = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    message     = models.TextField()
    payload_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes  = [models.Index(fields=["server", "timestamp"])]

    def __str__(self):
        return f"[{self.server.slug}] {self.event_type} – {self.message[:60]}"
