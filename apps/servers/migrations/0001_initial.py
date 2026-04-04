import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Server",
            fields=[
                ("id",                        models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("name",                      models.CharField(max_length=128)),
                ("slug",                      models.SlugField(unique=True)),
                ("game_type",                 models.CharField(max_length=32, default="other",
                                                choices=[("minecraft_java","Minecraft Java"),("minecraft_bedrock","Minecraft Bedrock"),
                                                         ("terraria","Terraria"),("factorio","Factorio"),("other","Ostatni")])),
                ("is_active",                 models.BooleanField(default=True)),
                ("working_directory",         models.CharField(max_length=512)),
                ("start_command",             models.CharField(max_length=512)),
                ("stop_command",              models.CharField(max_length=512, blank=True)),
                ("tmux_session_name",         models.CharField(max_length=64, unique=True)),
                ("log_file_path",             models.CharField(max_length=512)),
                ("pid_file_path",             models.CharField(max_length=512, blank=True)),
                ("host_label",                models.CharField(max_length=64, default="localhost")),
                ("rcon_enabled",              models.BooleanField(default=False)),
                ("rcon_host",                 models.CharField(max_length=128, blank=True)),
                ("rcon_port",                 models.IntegerField(null=True, blank=True)),
                ("rcon_password",             models.CharField(max_length=128, blank=True)),
                ("expected_startup_seconds",  models.IntegerField(default=60)),
                ("expected_shutdown_seconds", models.IntegerField(default=30)),
                ("status",                    models.CharField(max_length=16, default="OFFLINE",
                                                choices=[("OFFLINE","Offline"),("STARTING","Startuje"),("ONLINE","Online"),
                                                         ("STOPPING","Zastavuje se"),("CRASHED","Crashed"),("UNKNOWN","Nezname")])),
                ("last_seen_at",              models.DateTimeField(null=True, blank=True)),
                ("created_at",               models.DateTimeField(auto_now_add=True)),
                ("updated_at",               models.DateTimeField(auto_now=True)),
                ("webhook_url",              models.URLField(blank=True)),
                ("webhook_on_crash",         models.BooleanField(default=True)),
                ("webhook_on_start",         models.BooleanField(default=False)),
                ("webhook_on_stop",          models.BooleanField(default=False)),
                ("backup_directory",         models.CharField(max_length=512, blank=True)),
                ("backup_max_age_hours",     models.IntegerField(default=24)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="ServerProcessState",
            fields=[
                ("id",                   models.BigAutoField(primary_key=True, serialize=False)),
                ("server",               models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="process_state", to="servers.server")),
                ("pid",                  models.IntegerField(null=True, blank=True)),
                ("status",               models.CharField(max_length=16, default="OFFLINE")),
                ("started_at",           models.DateTimeField(null=True, blank=True)),
                ("stopped_at",           models.DateTimeField(null=True, blank=True)),
                ("last_healthcheck_at",  models.DateTimeField(null=True, blank=True)),
                ("last_log_line_at",     models.DateTimeField(null=True, blank=True)),
                ("last_command_at",      models.DateTimeField(null=True, blank=True)),
                ("last_player_count",    models.IntegerField(default=0)),
                ("cpu_percent_last",     models.FloatField(null=True, blank=True)),
                ("rss_bytes_last",       models.BigIntegerField(null=True, blank=True)),
                ("thread_count_last",    models.IntegerField(null=True, blank=True)),
                ("last_error",           models.TextField(blank=True)),
                ("consecutive_failures", models.IntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="PlayerSession",
            fields=[
                ("id",               models.BigAutoField(primary_key=True, serialize=False)),
                ("server",           models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                       related_name="player_sessions", to="servers.server")),
                ("player_name",      models.CharField(max_length=64, db_index=True)),
                ("joined_at",        models.DateTimeField(db_index=True)),
                ("left_at",          models.DateTimeField(null=True, blank=True)),
                ("duration_seconds", models.IntegerField(null=True, blank=True)),
            ],
            options={"ordering": ["-joined_at"]},
        ),
        migrations.AddIndex(
            model_name="playersession",
            index=models.Index(fields=["server", "joined_at"], name="ps_server_joined_idx"),
        ),
        migrations.AddIndex(
            model_name="playersession",
            index=models.Index(fields=["player_name"], name="ps_player_idx"),
        ),
    ]
