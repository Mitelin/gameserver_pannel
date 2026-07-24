import json
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.audit.models import AuditEvent
from apps.setup.models import BootstrapState
from apps.servers.backup_engine import create_backup
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
            "backup_exclude_paths": "",
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

    def test_backup_exclude_paths_rejects_absolute_path(self):
        with TemporaryDirectory() as temp_dir:
            server = self.make_server(working_directory=temp_dir)
            form = ServerForm(data={
                "name": server.name,
                "slug": server.slug,
                "game_type": server.game_type,
                "is_active": "on",
                "working_directory": temp_dir,
                "start_command": server.start_command,
                "stop_command": server.stop_command,
                "tmux_session_name": server.tmux_session_name,
                "log_file_path": server.log_file_path,
                "expected_startup_seconds": 60,
                "expected_shutdown_seconds": 30,
                "backup_directory": temp_dir,
                "backup_max_age_hours": 24,
                "backup_exclude_paths": "/etc/passwd",
            }, instance=server)

            self.assertFalse(form.is_valid())
            self.assertIn("backup_exclude_paths", form.errors)

    def test_backup_exclude_paths_rejects_traversal_outside(self):
        with TemporaryDirectory() as temp_dir:
            server = self.make_server(working_directory=temp_dir)
            form = ServerForm(data={
                "name": server.name,
                "slug": server.slug,
                "game_type": server.game_type,
                "is_active": "on",
                "working_directory": temp_dir,
                "start_command": server.start_command,
                "stop_command": server.stop_command,
                "tmux_session_name": server.tmux_session_name,
                "log_file_path": server.log_file_path,
                "expected_startup_seconds": 60,
                "expected_shutdown_seconds": 30,
                "backup_directory": temp_dir,
                "backup_max_age_hours": 24,
                "backup_exclude_paths": "../outside",
            }, instance=server)

            self.assertFalse(form.is_valid())
            self.assertIn("backup_exclude_paths", form.errors)

    def test_backup_exclude_paths_rejects_working_directory_itself(self):
        with TemporaryDirectory() as temp_dir:
            server = self.make_server(working_directory=temp_dir)
            form = ServerForm(data={
                "name": server.name,
                "slug": server.slug,
                "game_type": server.game_type,
                "is_active": "on",
                "working_directory": temp_dir,
                "start_command": server.start_command,
                "stop_command": server.stop_command,
                "tmux_session_name": server.tmux_session_name,
                "log_file_path": server.log_file_path,
                "expected_startup_seconds": 60,
                "expected_shutdown_seconds": 30,
                "backup_directory": temp_dir,
                "backup_max_age_hours": 24,
                "backup_exclude_paths": ".",
            }, instance=server)

            self.assertFalse(form.is_valid())
            self.assertIn("backup_exclude_paths", form.errors)

    def test_backup_exclude_paths_normalizes_empty_lines_and_duplicates(self):
        with TemporaryDirectory() as temp_dir:
            server = self.make_server(working_directory=temp_dir)
            form = ServerForm(data={
                "name": server.name,
                "slug": server.slug,
                "game_type": server.game_type,
                "is_active": "on",
                "working_directory": temp_dir,
                "start_command": server.start_command,
                "stop_command": server.stop_command,
                "tmux_session_name": server.tmux_session_name,
                "log_file_path": server.log_file_path,
                "expected_startup_seconds": 60,
                "expected_shutdown_seconds": 30,
                "backup_directory": temp_dir,
                "backup_max_age_hours": 24,
                "backup_exclude_paths": "\n dynmap/web/tiles \n\ndynmap/web/tiles\ndynmap/web/../web/tiles\n",
            }, instance=server)

            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["backup_exclude_paths"], "dynmap/web/tiles")


class BackupExclusionTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.working_dir = self.root / "gtnh-production"
        self.backup_dir = self.working_dir / "backups"
        (self.working_dir / "dynmap" / "web" / "tiles" / "day").mkdir(parents=True)
        (self.working_dir / "dynmap").mkdir(exist_ok=True)
        (self.working_dir / "Vyc").mkdir()
        self.backup_dir.mkdir(parents=True)
        (self.working_dir / "dynmap" / "web" / "tiles" / "day" / "0_0.png").write_text("tile", encoding="utf-8")
        (self.working_dir / "dynmap" / "configuration.txt").write_text("dynmap config", encoding="utf-8")
        (self.working_dir / "Vyc" / "level.dat").write_text("world", encoding="utf-8")
        (self.working_dir / "server.properties").write_text("motd=GTNH", encoding="utf-8")
        (self.backup_dir / "nested-should-not-archive.txt").write_text("skip me", encoding="utf-8")

        self.server = Server.objects.create(
            name="GTNH Production",
            slug="gtnh-production",
            game_type=GameType.MINECRAFT_JAVA,
            working_directory=str(self.working_dir),
            start_command="java -jar server.jar nogui",
            stop_command="stop",
            tmux_session_name="gtnh_production",
            log_file_path=str(self.working_dir / "logs" / "latest.log"),
            backup_directory=str(self.backup_dir),
            backup_max_age_hours=24,
            backup_exclude_paths="dynmap/web/tiles",
            status=ServerStatus.OFFLINE,
        )

    def _archive_members(self, archive_path: str) -> list[str]:
        with tarfile.open(archive_path, "r:gz") as tar:
            return tar.getnames()

    def test_auto_backup_respects_backup_exclusions_and_preserves_other_content(self):
        result = create_backup(self.server, is_user=False)

        self.assertTrue(result["ok"], result)
        names = self._archive_members(result["path"])
        self.assertNotIn("gtnh-production/dynmap/web/tiles", names)
        self.assertNotIn("gtnh-production/dynmap/web/tiles/day/0_0.png", names)
        self.assertIn("gtnh-production/dynmap/configuration.txt", names)
        self.assertIn("gtnh-production/Vyc/level.dat", names)
        self.assertIn("gtnh-production/server.properties", names)
        self.assertNotIn("gtnh-production/backups/nested-should-not-archive.txt", names)

    def test_user_backup_respects_backup_exclusions(self):
        result = create_backup(self.server, is_user=True)

        self.assertTrue(result["ok"], result)
        names = self._archive_members(result["path"])
        self.assertNotIn("gtnh-production/dynmap/web/tiles/day/0_0.png", names)
        self.assertIn("gtnh-production/dynmap/configuration.txt", names)

    def test_invalid_backup_exclusion_config_fails_without_archive(self):
        self.server.backup_exclude_paths = "/etc/passwd"
        self.server.save(update_fields=["backup_exclude_paths"])

        result = create_backup(self.server, is_user=False)

        self.assertFalse(result["ok"])
        self.assertEqual(list(self.backup_dir.glob("*.tar.gz")), [])

    def test_partial_archive_is_removed_after_backup_failure(self):
        def _failing_archive(*args, **kwargs):
            dest = args[2]
            dest.write_bytes(b"partial")
            raise RuntimeError("boom")

        with mock.patch("apps.servers.backup_engine._archive_server_files", side_effect=_failing_archive):
            result = create_backup(self.server, is_user=False)

        self.assertFalse(result["ok"])
        self.assertEqual(list(self.backup_dir.glob("*.tar.gz")), [])


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
            backup_exclude_paths="",
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
            "backup_exclude_paths": "dynmap/web/tiles",
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

    def test_server_edit_persists_backup_exclude_paths(self):
        response = self.client.post(f"/servers/{self.server.slug}/edit/", self.post_data())

        self.assertEqual(response.status_code, 302)
        self.server.refresh_from_db()
        self.assertEqual(self.server.backup_exclude_paths, "dynmap/web/tiles")

    def test_server_edit_can_disable_rcon_without_losing_password(self):
        response = self.client.post(f"/servers/{self.server.slug}/edit/", self.post_data())

        self.assertEqual(response.status_code, 302)
        self.server.refresh_from_db()
        self.assertFalse(self.server.rcon_enabled)
        self.assertEqual(self.server.rcon_password, "super-secret")
        event = AuditEvent.objects.filter(server=self.server, event_type="server.config.changed").latest("timestamp")
        self.assertNotIn("super-secret", event.message)
        self.assertNotIn("super-secret", str(event.payload_json))

    def test_audit_records_backup_exclude_path_change(self):
        self.client.post(f"/servers/{self.server.slug}/edit/", self.post_data())

        event = AuditEvent.objects.filter(server=self.server, event_type="server.config.changed").latest("timestamp")
        self.assertIn("backup_exclude_paths", event.payload_json["changed"])
        self.assertEqual(event.payload_json["changed"]["backup_exclude_paths"]["new"], "dynmap/web/tiles")

    def test_export_import_preserves_backup_exclude_paths(self):
        self.server.backup_exclude_paths = "dynmap/web/tiles"
        self.server.save(update_fields=["backup_exclude_paths"])

        export_response = self.client.get(f"/servers/{self.server.slug}/export/")
        self.assertEqual(export_response.status_code, 200)
        exported = json.loads(export_response.content.decode("utf-8"))
        self.assertEqual(exported["backup_exclude_paths"], "dynmap/web/tiles")

        exported["slug"] = "gtnh-production-imported"
        exported["name"] = "GTNH Imported"
        upload = SimpleUploadedFile("server.json", json.dumps(exported).encode("utf-8"), content_type="application/json")

        import_response = self.client.post("/servers/import/", {"config_file": upload})
        self.assertEqual(import_response.status_code, 302)
        imported = Server.objects.get(slug="gtnh-production-imported")
        self.assertEqual(imported.backup_exclude_paths, "dynmap/web/tiles")

    def test_template_contains_rcon_sync_and_invalid_details_handler(self):
        template_path = Path(__file__).resolve().parents[2] / "templates" / "servers" / "server_edit.html"
        content = template_path.read_text(encoding="utf-8")

        self.assertIn("field.disabled = !enabled", content)
        self.assertIn("portField.value = '25575'", content)
        self.assertIn("serverForm.addEventListener('invalid', revealInvalidDetails, true)", content)
        self.assertIn("collapsedDetails.open = true", content)
        self.assertIn("advanced-settings", content)
        self.assertIn("Vyloučené cesty ze zálohy", content)
        self.assertIn("backup_exclude_paths.help_text", content)