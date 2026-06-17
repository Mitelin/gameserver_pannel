from django.db import migrations


def seed_desired_running(apps, schema_editor):
    Server = apps.get_model("servers", "Server")
    Server.objects.filter(status__in=["ONLINE", "STARTING"]).update(desired_running=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("servers", "0009_server_desired_running"),
    ]

    operations = [
        migrations.RunPython(seed_desired_running, noop),
    ]