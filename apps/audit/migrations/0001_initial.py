import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("servers", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id",           models.BigAutoField(primary_key=True, serialize=False)),
                ("server",       models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                   related_name="audit_events", to="servers.server")),
                ("timestamp",    models.DateTimeField(auto_now_add=True, db_index=True)),
                ("event_type",   models.CharField(max_length=64, db_index=True)),
                ("severity",     models.CharField(max_length=8, default="info",
                                   choices=[("info","Info"),("warning","Varování"),("error","Chyba")])),
                ("user",         models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL,
                                   to=settings.AUTH_USER_MODEL)),
                ("message",      models.TextField()),
                ("payload_json", models.JSONField(default=dict, blank=True)),
            ],
            options={"ordering": ["-timestamp"]},
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["server", "timestamp"], name="audit_server_ts_idx"),
        ),
    ]
