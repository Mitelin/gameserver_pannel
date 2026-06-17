from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("servers", "0008_tmux_optional"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="desired_running",
            field=models.BooleanField(
                default=False,
                help_text="Persisted desired state. True = panel should keep this server running and restore it after crash or host restart.",
            ),
        ),
    ]