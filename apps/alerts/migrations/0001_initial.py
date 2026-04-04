import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("servers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AlertRule",
            fields=[
                ("id",               models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("server",           models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                       related_name="alert_rules", to="servers.server")),
                ("name",             models.CharField(max_length=128)),
                ("is_active",        models.BooleanField(default=True)),
                ("condition_type",   models.CharField(max_length=32,
                                       choices=[("status_change","Změna stavu"),("cpu_threshold","CPU limit"),
                                                ("ram_threshold","RAM limit"),("no_players","Žádní hráči"),
                                                ("log_pattern","Pattern v logu")])),
                ("status_values",    models.CharField(max_length=128, blank=True)),
                ("threshold_value",  models.FloatField(null=True, blank=True)),
                ("duration_minutes", models.IntegerField(null=True, blank=True)),
                ("log_pattern",      models.CharField(max_length=256, blank=True)),
                ("channel",          models.CharField(max_length=16, default="webhook",
                                       choices=[("webhook","Webhook")])),
                ("webhook_url",      models.URLField()),
                ("message_template", models.TextField(
                    default="🚨 [{server_name}] Alert: {condition} – {details}")),
                ("cooldown_minutes", models.IntegerField(default=15)),
                ("created_at",       models.DateTimeField(auto_now_add=True)),
                ("updated_at",       models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["server", "name"]},
        ),
        migrations.CreateModel(
            name="AlertFire",
            fields=[
                ("id",       models.BigAutoField(primary_key=True, serialize=False)),
                ("rule",     models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                               related_name="fires", to="alerts.alertrule")),
                ("fired_at", models.DateTimeField(auto_now_add=True)),
                ("details",  models.TextField(blank=True)),
                ("sent_ok",  models.BooleanField(default=True)),
            ],
            options={"ordering": ["-fired_at"]},
        ),
    ]
