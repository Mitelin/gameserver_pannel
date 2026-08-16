from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.dashboard.views import _build_update_shell_command, _parse_restart_services, _sort_backups_for_overview, _update_restart_plan
from apps.setup.models import BootstrapState
from apps.servers.models import GameType, Server, ServerStatus

User = get_user_model()


class UpdateRestartPlanTests(SimpleTestCase):
    def test_parse_restart_services_splits_and_deduplicates(self):
        services = _parse_restart_services(
            "gameserver-panel-worker.service, gameserver-panel-web.service\n"
            "gameserver-panel-worker.service; gameserver-panel-watchdog.service"
        )

        self.assertEqual(
            services,
            [
                "gameserver-panel-worker.service",
                "gameserver-panel-web.service",
                "gameserver-panel-watchdog.service",
            ],
        )

    @patch("apps.dashboard.views.shutil.which", return_value="/bin/systemctl")
    @patch.dict(
        "apps.dashboard.views.os.environ",
        {
            "GAMEPANEL_UPDATE_WEB_SERVICE": "gameserver-panel-web.service",
            "GAMEPANEL_UPDATE_WORKER_SERVICE": "gameserver-panel-worker.service",
        },
        clear=True,
    )
    def test_default_systemd_plan_restarts_web_runtime_and_legacy_workers(self, _which):
        plan = _update_restart_plan()

        self.assertEqual(plan["mode"], "systemd")
        self.assertEqual(
            plan["services"],
            [
                "gameserver-panel-worker.service",
                "gameserver-panel-web.service",
                "gameserver-panel-console-tail.service",
                "gameserver-panel-metrics.service",
                "gameserver-panel-watchdog.service",
            ],
        )

    @patch("apps.dashboard.views.shutil.which", return_value="/bin/systemctl")
    @patch.dict(
        "apps.dashboard.views.os.environ",
        {
            "GAMEPANEL_UPDATE_RESTART_SERVICES": (
                "alpha.service beta.service alpha.service gameserver-panel-web.service"
            ),
            "GAMEPANEL_UPDATE_WEB_SERVICE": "gameserver-panel-web.service",
            "GAMEPANEL_UPDATE_WORKER_SERVICE": "",
        },
        clear=True,
    )
    def test_env_service_list_overrides_defaults(self, _which):
        plan = _update_restart_plan()

        self.assertEqual(plan["services"], ["alpha.service", "beta.service", "gameserver-panel-web.service"])

    @patch("apps.dashboard.views.shutil.which", return_value="/bin/systemctl")
    @patch.dict(
        "apps.dashboard.views.os.environ",
        {
            "GAMEPANEL_UPDATE_RESTART_SERVICES": "alpha.service beta.service",
            "GAMEPANEL_UPDATE_WEB_SERVICE": "web.service",
            "GAMEPANEL_UPDATE_WORKER_SERVICE": "worker.service",
        },
        clear=True,
    )
    def test_update_shell_command_restarts_all_configured_services(self, _which):
        command = _build_update_shell_command()

        self.assertIn("[update] restarting worker.service", command)
        self.assertIn("sudo -n /bin/systemctl restart worker.service", command)
        self.assertIn("/bin/systemctl restart worker.service", command)
        self.assertIn("[update] restarting alpha.service", command)
        self.assertIn("sudo -n /bin/systemctl restart alpha.service", command)
        self.assertIn("/bin/systemctl restart alpha.service", command)
        self.assertIn("[update] restarting beta.service", command)
        self.assertIn("sudo -n /bin/systemctl restart beta.service", command)
        self.assertIn("/bin/systemctl restart beta.service", command)
        self.assertIn("[update] restarting web.service", command)
        self.assertIn("sudo -n /bin/systemctl restart web.service", command)
        self.assertIn("/bin/systemctl restart web.service", command)

    @patch("apps.dashboard.views.shutil.which", return_value="/bin/systemctl")
    @patch.dict(
        "apps.dashboard.views.os.environ",
        {
            "GAMEPANEL_UPDATE_WEB_SERVICE": "gameserver-panel-web.service",
            "GAMEPANEL_UPDATE_WORKER_SERVICE": "gameserver-panel-worker.service",
        },
        clear=True,
    )
    def test_update_shell_command_uses_fetch_plus_fast_forward_merge(self, _which):
        command = _build_update_shell_command()

        self.assertIn('git fetch --prune origin "$BRANCH"', command)
        self.assertIn("git merge --ff-only FETCH_HEAD", command)
        self.assertNotIn("git pull --ff-only", command)


class BackupOverviewTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.backup_dir = Path(self.temp_dir.name)
        BootstrapState.objects.create(is_completed=True)
        self.user = User.objects.create_superuser(username="admin", email="admin@example.com", password="secret123")
        self.client.force_login(self.user)
        self.server = Server.objects.create(
            name="GTNH Production",
            slug="gtnh-production",
            game_type=GameType.MINECRAFT_JAVA,
            working_directory=str(self.backup_dir / "work"),
            start_command="java -jar server.jar nogui",
            stop_command="stop",
            tmux_session_name="gtnh_production",
            log_file_path=str(self.backup_dir / "logs" / "latest.log"),
            backup_directory=str(self.backup_dir),
            backup_max_age_hours=24,
            backup_exclude_paths="",
            status=ServerStatus.OFFLINE,
        )

    def test_default_sort_orders_by_kind_then_created_desc(self):
        now = timezone.now()
        backups = [
            {"name": "user-new", "kind": "USER", "retention_bucket": "user", "created_at_dt": now},
            {"name": "auto-old", "kind": "AUTO", "retention_bucket": "daily", "created_at_dt": now - timedelta(hours=3)},
            {"name": "auto-new", "kind": "AUTO", "retention_bucket": "hourly", "created_at_dt": now - timedelta(hours=1)},
        ]

        sorted_backups = _sort_backups_for_overview(backups)

        self.assertEqual([item["name"] for item in sorted_backups], ["auto-new", "auto-old", "user-new"])

    def test_backup_download_returns_selected_archive(self):
        archive = self.backup_dir / "hourly" / "gtnh-production-20260816-203359-HOURLY.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"backup-bytes")

        response = self.client.get(reverse("dashboard:backup_download", args=[self.server.slug]), {"name": archive.name})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="{archive.name}"')
        self.assertEqual(b"".join(response.streaming_content), b"backup-bytes")