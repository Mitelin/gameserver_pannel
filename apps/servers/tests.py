from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.audit.models import AuditEvent
from apps.setup.models import BootstrapState
from apps.servers.forms import ServerForm
from apps.servers.models import GameType, Server, ServerStatus


User = get_user_model()


class ServerFormTests(TestCase):
    def make_server(self, **overrides):
        params = {
            "name": "GTNH Production",
            "slug": "gtnh-production",
            "game_type": GameType.MINECRAFT_JAVA,
            "working_directory": "/srv/minecraft/gtnh-production",
            "start_command": "java -jar server.jar nogui",
            "stop_command": "stop",
            "tmux_session_name": "gtnh_production",
            "log_file_path": "/srv/minecraft/gtnh-production/logs/latest.log",
            "backup_directory": "/srv/minecraft/gtnh-production/backups-old",
            "backup_max_age_hours": 24,
            "rcon_enabled": True,
            "rcon_host": "127.0.0.1",
            "rcon_port": 25575,
            "rcon_password": "super-secret",
            "status": ServerStatus.OFFLINE,
        }
        params.update(overrides)
        return Server.objects.create(**params)

    def test_edit_with_disabled_rcon_and_empty_port_is_valid(self):
        server = self.make_server()
        form = ServerForm(data={
            "name": server.name,
            "slug": server.slug,
            "game_type": server.game_type,
            "is_active": "on",
            "working_directory": server.working_directory,
            "start_command": server.start_command,
            "stop_command": server.stop_command,
            "tmux_session_name": server.tmux_session_name,
            "log_file_path": server.log_file_path,
            "expected_startup_seconds": 60,
            "expected_shutdown_seconds": 30,
            "rcon_host": "",
            "rcon_port": "",
            "rcon_password": "",
            "backup_directory": "/srv/minecraft/gtnh-production/backups",
            "backup_max_age_hours": 48,
        }, instance=server)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["rcon_enabled"])
        self.assertEqual(form.cleaned_data["rcon_port"], 25575)
        self.assertEqual(form.cleaned_data["rcon_password"], "super-secret")

    def test_previously_enabled_rcon_can_be_turned_off(self):
        server = self.make_server(rcon_enabled=True)
        form = ServerForm(data={
            "name": server.name,
            "slug": server.slug,
            "game_type": server.game_type,
            "is_active": "on",
            "working_directory": server.working_directory,
            "start_command": server.start_command,
            "stop_command": server.stop_command,
            "tmux_session_name": server.tmux_session_name,
            "log_file_path": server.log_file_path,
            "expected_startup_seconds": 60,
            "expected_shutdown_seconds": 30,
            "backup_directory": server.backup_directory,
            "backup_max_age_hours": server.backup_max_age_hours,
        }, instance=server)

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertFalse(saved.rcon_enabled)
        self.assertEqual(saved.rcon_password, "super-secret")

    def test_enabled_rcon_with_invalid_port_is_rejected(self):
        server = self.make_server()
        form = ServerForm(data={
            "name": server.name,
            "slug": server.slug,
            "game_type": server.game_type,
            "is_active": "on",
            "working_directory": server.working_directory,
            "start_command": server.start_command,
            "stop_command": server.stop_command,
            "tmux_session_name": server.tmux_session_name,
            "log_file_path": server.log_file_path,
            "expected_startup_seconds": 60,
            "expected_shutdown_seconds": 30,
            "rcon_enabled": "on",
            "rcon_host": "127.0.0.1",
            "rcon_port": "70000",
            "rcon_password": "super-secret",
            "backup_directory": server.backup_directory,
            "backup_max_age_hours": server.backup_max_age_hours,
        }, instance=server)

        self.assertFalse(form.is_valid())
        self.assertIn("rcon_port", form.errors)

    def test_enabled_rcon_with_empty_port_defaults_to_25575(self):
        server = self.make_server(rcon_port=None)
        form = ServerForm(data={
            "name": server.name,
            "slug": server.slug,
            "game_type": server.game_type,
            "is_active": "on",
            "working_directory": server.working_directory,
            "start_command": server.start_command,
            "stop_command": server.stop_command,
            "tmux_session_name": server.tmux_session_name,
            "log_file_path": server.log_file_path,
            "expected_startup_seconds": 60,
            "expected_shutdown_seconds": 30,
            "rcon_enabled": "on",
            "rcon_host": "127.0.0.1",
            "rcon_port": "",
            "rcon_password": "super-secret",
            "backup_directory": server.backup_directory,
            "backup_max_age_hours": server.backup_max_age_hours,
        }, instance=server)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["rcon_port"], 25575)


class ServerEditTests(TestCase):
    def setUp(self):
        BootstrapState.objects.create(is_completed=True)
        self.user = User.objects.create_superuser(username="admin", email="admin@example.com", password="secret123")
        self.client.force_login(self.user)
        self.server = Server.objects.create(
            name="GTNH Production",
            slug="gtnh-production",
            game_type=GameType.MINECRAFT_JAVA,
            working_directory="/srv/minecraft/gtnh-production",
            start_command="java -jar server.jar nogui",
            stop_command="stop",
            tmux_session_name="gtnh_production",
            log_file_path="/srv/minecraft/gtnh-production/logs/latest.log",
            backup_directory="/srv/minecraft/gtnh-production/backups-old",
            backup_max_age_hours=24,
            rcon_enabled=True,
            rcon_host="127.0.0.1",
            rcon_port=25575,
            rcon_password="super-secret",
            status=ServerStatus.OFFLINE,
        )

    def post_data(self, **overrides):
        data = {
            "name": self.server.name,
            "slug": self.server.slug,
            "game_type": self.server.game_type,
            "is_active": "on",
            "working_directory": self.server.working_directory,
            "start_command": self.server.start_command,
            "stop_command": self.server.stop_command,
            "tmux_session_name": self.server.tmux_session_name,
            "log_file_path": self.server.log_file_path,
            "expected_startup_seconds": 60,
            "expected_shutdown_seconds": 30,
            "backup_directory": "/srv/minecraft/gtnh-production/backups",
            "backup_max_age_hours": 72,
            "webhook_url": "",
        }
        data.update(overrides)
        return data

    def test_server_edit_persists_backup_directory_and_redirects(self):
        response = self.client.post(f"/servers/{self.server.slug}/edit/", self.post_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/servers/{self.server.slug}/edit/")
        self.server.refresh_from_db()
        self.assertEqual(self.server.backup_directory, "/srv/minecraft/gtnh-production/backups")
        self.assertEqual(self.server.backup_max_age_hours, 72)

    def test_server_edit_can_disable_rcon_without_losing_password(self):
        response = self.client.post(f"/servers/{self.server.slug}/edit/", self.post_data())

        self.assertEqual(response.status_code, 302)
        self.server.refresh_from_db()
        self.assertFalse(self.server.rcon_enabled)
        self.assertEqual(self.server.rcon_password, "super-secret")
        event = AuditEvent.objects.filter(server=self.server, event_type="server.config.changed").latest("timestamp")
        self.assertNotIn("super-secret", event.message)
        self.assertNotIn("super-secret", str(event.payload_json))

    def test_template_contains_rcon_sync_and_invalid_details_handler(self):
        template_path = Path(__file__).resolve().parents[2] / "templates" / "servers" / "server_edit.html"
        content = template_path.read_text(encoding="utf-8")

        self.assertIn("field.disabled = !enabled", content)
        self.assertIn("portField.value = '25575'", content)
        self.assertIn("serverForm.addEventListener('invalid', revealInvalidDetails, true)", content)
        self.assertIn("collapsedDetails.open = true", content)
        self.assertIn("advanced-settings", content)