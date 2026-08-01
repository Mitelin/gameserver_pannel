import json
import tarfile
from collections import Counter
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.setup.models import BootstrapState
from apps.servers.backup import AUTO_BACKUP_INTERVAL_HOURS, check_auto_backup_due, check_backup_status
from apps.servers.backup_engine import create_backup, list_backups, rotate_backups
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


class AutoBackupScheduleTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.backup_dir = Path(self.temp_dir.name)
        self.server = Server.objects.create(
            name="GTNH Production",
            slug="gtnh-production",
            game_type=GameType.MINECRAFT_JAVA,
            working_directory="/srv/minecraft/gtnh-production",
            start_command="java -jar server.jar nogui",
            stop_command="stop",
            tmux_session_name="gtnh_production",
            log_file_path="/srv/minecraft/gtnh-production/logs/latest.log",
            backup_directory=str(self.backup_dir),
            backup_max_age_hours=24,
            backup_exclude_paths="",
            status=ServerStatus.OFFLINE,
        )
        self.now = timezone.make_aware(timezone.datetime(2026, 7, 24, 12, 0, 0), timezone.get_current_timezone())

    def _create_archive(self, dt, *, is_user=False, name=None):
        if name is None:
            suffix = "-USER" if is_user else ""
            name = f"{self.server.slug}-{dt.strftime('%Y%m%d-%H%M%S')}{suffix}.tar.gz"
        archive = self.backup_dir / name
        archive.write_bytes(b"backup")
        return archive

    def test_no_archive_means_auto_backup_is_due(self):
        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["ok"])
        self.assertTrue(result["due"])

    def test_recent_auto_backup_is_not_due(self):
        self._create_archive(self.now - timedelta(hours=2, minutes=59))

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["ok"])
        self.assertFalse(result["due"])
        self.assertEqual(result["newest_kind"], "AUTO")

    def test_auto_backup_exactly_three_hours_old_is_due(self):
        self._create_archive(self.now - timedelta(hours=AUTO_BACKUP_INTERVAL_HOURS))

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])

    def test_auto_backup_older_than_three_hours_is_due(self):
        self._create_archive(self.now - timedelta(hours=4))

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])

    def test_recent_user_backup_is_not_due(self):
        self._create_archive(self.now - timedelta(hours=2), is_user=True)

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertFalse(result["due"])
        self.assertEqual(result["newest_kind"], "USER")

    def test_old_user_backup_is_due(self):
        self._create_archive(self.now - timedelta(hours=4), is_user=True)

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])
        self.assertEqual(result["newest_kind"], "USER")

    def test_newer_user_backup_wins_over_older_auto_backup(self):
        self._create_archive(self.now - timedelta(hours=5))
        self._create_archive(self.now - timedelta(hours=2), is_user=True)

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertFalse(result["due"])
        self.assertEqual(result["newest_kind"], "USER")

    def test_newer_auto_backup_wins_over_older_user_backup(self):
        self._create_archive(self.now - timedelta(hours=5), is_user=True)
        self._create_archive(self.now - timedelta(hours=2))

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertFalse(result["due"])
        self.assertEqual(result["newest_kind"], "AUTO")

    def test_unrelated_tar_gz_does_not_affect_due_decision(self):
        self._create_archive(self.now - timedelta(minutes=30), name="gtnh-production-manual-export.tar.gz")
        self._create_archive(self.now - timedelta(hours=4), name="other-server-20260724-080000.tar.gz")

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])
        self.assertNotIn("newest_file", result)

    def test_backup_max_age_hours_does_not_change_three_hour_due_interval(self):
        self._create_archive(self.now - timedelta(hours=4))

        due_result = check_auto_backup_due(self.server, now=self.now)
        status_result = check_backup_status(self.server, now=self.now)

        self.assertTrue(due_result["due"])
        self.assertTrue(status_result["ok"])
        self.assertEqual(status_result["max_age_hours"], 24)

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


RETENTION_REPRO_TIMESTAMPS = [
    "20260724-100957", "20260724-143341", "20260724-173701", "20260724-204011", "20260724-234324",
    "20260725-024649", "20260725-055011", "20260725-085330", "20260725-115645", "20260725-145959",
    "20260725-180318", "20260725-210626", "20260726-000935", "20260726-031246", "20260726-061607",
    "20260726-091927", "20260726-122244", "20260726-152556", "20260726-182916", "20260726-213226",
    "20260727-003545", "20260727-033905", "20260727-064221", "20260727-094539", "20260727-124848",
    "20260727-155200", "20260727-185509", "20260727-215816", "20260728-010152", "20260728-040518",
    "20260728-070856", "20260728-101011", "20260728-131341", "20260728-161716", "20260728-192038",
    "20260728-222406", "20260729-012728", "20260729-043052", "20260729-073415", "20260729-103745",
    "20260729-134115", "20260729-164445", "20260729-194816", "20260729-225145", "20260730-015513",
    "20260730-045837", "20260730-080159", "20260730-110525", "20260730-140852", "20260730-171213",
    "20260730-201534", "20260730-231858", "20260731-022220", "20260731-052543", "20260731-082906",
    "20260731-113410", "20260731-143735", "20260731-174102", "20260731-204425", "20260731-234750",
    "20260801-025112", "20260801-055435", "20260801-085758", "20260801-120123", "20260801-150450",
]

EXPECTED_LAYERED_KEEP = [
    ("gtnh-production-20260801-150450.tar.gz", "intraday"),
    ("gtnh-production-20260801-120123.tar.gz", "intraday"),
    ("gtnh-production-20260801-085758.tar.gz", "intraday"),
    ("gtnh-production-20260801-055435.tar.gz", "intraday"),
    ("gtnh-production-20260801-025112.tar.gz", "intraday"),
    ("gtnh-production-20260731-234750.tar.gz", "intraday"),
    ("gtnh-production-20260731-204425.tar.gz", "intraday"),
    ("gtnh-production-20260731-174102.tar.gz", "intraday"),
    ("gtnh-production-20260731-143735.tar.gz", "daily"),
    ("gtnh-production-20260730-231858.tar.gz", "daily"),
    ("gtnh-production-20260729-225145.tar.gz", "daily"),
    ("gtnh-production-20260728-222406.tar.gz", "daily"),
    ("gtnh-production-20260727-215816.tar.gz", "daily"),
    ("gtnh-production-20260726-213226.tar.gz", "daily"),
    ("gtnh-production-20260725-210626.tar.gz", "daily"),
    ("gtnh-production-20260725-145959.tar.gz", "weekly"),
]


class BackupRetentionTests(TestCase):
    def _make_server(self, directory: Path, *, backup_keep_count: int = 12) -> Server:
        return Server.objects.create(
            name="GTNH Production Retention",
            slug="gtnh-production-retention",
            game_type=GameType.MINECRAFT_JAVA,
            working_directory=str(directory),
            start_command="java -jar server.jar nogui",
            stop_command="stop",
            tmux_session_name="gtnh_production_retention",
            log_file_path=str(directory / "logs" / "latest.log"),
            backup_directory=str(directory),
            backup_max_age_hours=24,
            backup_keep_count=backup_keep_count,
            backup_exclude_paths="",
            status=ServerStatus.OFFLINE,
        )

    def _create_backup_files(self, directory: Path, slug: str) -> None:
        for stamp in RETENTION_REPRO_TIMESTAMPS:
            (directory / f"{slug}-{stamp}.tar.gz").write_bytes(b"x")

    def test_rotate_backups_keeps_layered_retention_for_production_timestamp_series(self):
        with TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            server = self._make_server(backup_dir, backup_keep_count=12)
            self._create_backup_files(backup_dir, server.slug)

            rotation = rotate_backups(server)
            backups_after_rotation = list_backups(server)
            kept = [
                (item["name"], item["retention_bucket"])
                for item in backups_after_rotation
                if item["protected_by_rotation"]
            ]
            expected_kept = [(name.replace("gtnh-production", server.slug, 1), bucket) for name, bucket in EXPECTED_LAYERED_KEEP]

            self.assertEqual(rotation["rotated"], len(RETENTION_REPRO_TIMESTAMPS) - len(EXPECTED_LAYERED_KEEP))
            self.assertEqual(rotation["kept_total"], len(EXPECTED_LAYERED_KEEP))
            self.assertEqual(rotation["kept_intraday"], 8)
            self.assertEqual(rotation["kept_daily"], 7)
            self.assertEqual(rotation["kept_weekly"], 1)
            self.assertEqual(rotation["kept_monthly"], 0)
            self.assertEqual(len(backups_after_rotation), len(EXPECTED_LAYERED_KEEP))
            self.assertEqual(kept, expected_kept)
            self.assertEqual(
                Counter(item["retention_bucket"] for item in backups_after_rotation),
                Counter({"intraday": 8, "daily": 7, "weekly": 1}),
            )
