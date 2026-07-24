from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("servers", "0010_seed_desired_running"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="backup_exclude_paths",
            field=models.TextField(blank=True, default=""),
        ),
    ]