import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("servers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MetricSample",
            fields=[
                ("id",                 models.BigAutoField(primary_key=True, serialize=False)),
                ("server",             models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                         related_name="metric_samples", to="servers.server")),
                ("timestamp",          models.DateTimeField(db_index=True)),
                ("cpu_percent",        models.FloatField(null=True)),
                ("ram_bytes",          models.BigIntegerField(null=True)),
                ("system_cpu_percent", models.FloatField(null=True)),
                ("system_ram_bytes",   models.BigIntegerField(null=True)),
                ("thread_count",       models.IntegerField(null=True)),
                ("open_files",         models.IntegerField(null=True)),
                ("disk_used_bytes",    models.BigIntegerField(null=True)),
                ("disk_free_bytes",    models.BigIntegerField(null=True)),
                ("net_rx_bytes",       models.BigIntegerField(null=True)),
                ("net_tx_bytes",       models.BigIntegerField(null=True)),
                ("player_count",       models.IntegerField(null=True)),
                ("max_players",        models.IntegerField(null=True)),
                ("tps",                models.FloatField(null=True)),
            ],
            options={"ordering": ["timestamp"]},
        ),
        migrations.AddIndex(
            model_name="metricsample",
            index=models.Index(fields=["server", "timestamp"], name="metrics_sample_server_ts"),
        ),
        migrations.CreateModel(
            name="MetricMinute",
            fields=[
                ("id",           models.BigAutoField(primary_key=True, serialize=False)),
                ("server",       models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                   related_name="metric_minutes", to="servers.server")),
                ("timestamp",    models.DateTimeField(db_index=True)),
                ("cpu_avg",      models.FloatField(null=True)),
                ("cpu_max",      models.FloatField(null=True)),
                ("ram_avg",      models.BigIntegerField(null=True)),
                ("ram_max",      models.BigIntegerField(null=True)),
                ("player_avg",   models.FloatField(null=True)),
                ("player_max",   models.IntegerField(null=True)),
                ("thread_avg",   models.FloatField(null=True)),
                ("sample_count", models.IntegerField(default=0)),
            ],
            options={"ordering": ["timestamp"]},
        ),
        migrations.AlterUniqueTogether(
            name="metricminute",
            unique_together={("server", "timestamp")},
        ),
        migrations.AddIndex(
            model_name="metricminute",
            index=models.Index(fields=["server", "timestamp"], name="metrics_minute_server_ts"),
        ),
        migrations.CreateModel(
            name="MetricHour",
            fields=[
                ("id",           models.BigAutoField(primary_key=True, serialize=False)),
                ("server",       models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                   related_name="metric_hours", to="servers.server")),
                ("timestamp",    models.DateTimeField(db_index=True)),
                ("cpu_avg",      models.FloatField(null=True)),
                ("cpu_max",      models.FloatField(null=True)),
                ("ram_avg",      models.BigIntegerField(null=True)),
                ("ram_max",      models.BigIntegerField(null=True)),
                ("player_avg",   models.FloatField(null=True)),
                ("player_max",   models.IntegerField(null=True)),
                ("sample_count", models.IntegerField(default=0)),
            ],
            options={"ordering": ["timestamp"]},
        ),
        migrations.AlterUniqueTogether(
            name="metrichour",
            unique_together={("server", "timestamp")},
        ),
        migrations.AddIndex(
            model_name="metrichour",
            index=models.Index(fields=["server", "timestamp"], name="metrics_hour_server_ts"),
        ),
    ]
