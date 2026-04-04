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
            name="ConsoleLine",
            fields=[
                ("id",              models.BigAutoField(primary_key=True, serialize=False)),
                ("server",          models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                      related_name="console_lines", to="servers.server")),
                ("timestamp",       models.DateTimeField(auto_now_add=True, db_index=True)),
                ("line",            models.TextField()),
                ("stream_type",     models.CharField(max_length=8, default="stdout",
                                      choices=[("stdout","stdout"),("stderr","stderr"),("system","Systém")])),
                ("source",          models.CharField(max_length=16, default="log_tail",
                                      choices=[("log_tail","Log tail"),("system","Systém"),("synthetic","Syntetický")])),
                ("sequence_number", models.BigIntegerField(default=0)),
            ],
            options={"ordering": ["timestamp", "sequence_number"]},
        ),
        migrations.AddIndex(
            model_name="consoleline",
            index=models.Index(fields=["server", "timestamp"], name="console_server_ts_idx"),
        ),
        migrations.CreateModel(
            name="CommandHistory",
            fields=[
                ("id",             models.BigAutoField(primary_key=True, serialize=False)),
                ("server",         models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                     related_name="command_history", to="servers.server")),
                ("user",           models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL,
                                     to=settings.AUTH_USER_MODEL)),
                ("issued_at",      models.DateTimeField(auto_now_add=True, db_index=True)),
                ("command",        models.CharField(max_length=1024)),
                ("source",         models.CharField(max_length=16, default="web_console",
                                     choices=[("web_console","Web konzole"),("action_button","Tlačítko akce"),("system","Systém")])),
                ("accepted_at",    models.DateTimeField(null=True, blank=True)),
                ("dispatched_at",  models.DateTimeField(null=True, blank=True)),
                ("result_status",  models.CharField(max_length=16, default="ACCEPTED",
                                     choices=[("ACCEPTED","Přijato"),("DISPATCHED","Odesláno"),
                                              ("OBSERVED","Potvrzeno"),("FAILED","Selhalo"),("TIMEOUT","Timeout")])),
                ("result_message", models.TextField(blank=True)),
                ("correlation_id", models.CharField(max_length=64, blank=True, db_index=True)),
            ],
            options={"ordering": ["-issued_at"]},
        ),
    ]
