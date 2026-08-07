from unittest.mock import patch

from django.test import SimpleTestCase

from apps.dashboard.views import _build_update_shell_command, _parse_restart_services, _update_restart_plan


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