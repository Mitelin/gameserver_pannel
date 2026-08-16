import json
import os
import tarfile
from collections import Counter
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.setup.models import BootstrapState
from apps.servers.backup import AUTO_BACKUP_INTERVAL_HOURS, check_auto_backup_due, check_backup_layers_due, check_backup_status
from apps.servers.backup_engine import (
    BACKUP_KIND_DAILY,
    BACKUP_KIND_HOURLY,
    BACKUP_KIND_MONTHLY,
    BACKUP_KIND_USER,
    BACKUP_KIND_WEEKLY,
    create_backup,
    list_backups,
    rotate_backups,
)
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
        result = create_backup(self.server, backup_kind=BACKUP_KIND_HOURLY)

        self.assertTrue(result["ok"], result)
        names = self._archive_members(result["path"])
        self.assertNotIn("gtnh-production/dynmap/web/tiles", names)
        self.assertNotIn("gtnh-production/dynmap/web/tiles/day/0_0.png", names)
        self.assertIn("gtnh-production/dynmap/configuration.txt", names)
        self.assertIn("gtnh-production/Vyc/level.dat", names)
        self.assertIn("gtnh-production/server.properties", names)
        self.assertNotIn("gtnh-production/backups/nested-should-not-archive.txt", names)

    def test_user_backup_respects_backup_exclusions(self):
        result = create_backup(self.server, backup_kind=BACKUP_KIND_USER)

        self.assertTrue(result["ok"], result)
        names = self._archive_members(result["path"])
        self.assertNotIn("gtnh-production/dynmap/web/tiles/day/0_0.png", names)
        self.assertIn("gtnh-production/dynmap/configuration.txt", names)

    def test_invalid_backup_exclusion_config_fails_without_archive(self):
        self.server.backup_exclude_paths = "/etc/passwd"
        self.server.save(update_fields=["backup_exclude_paths"])

        result = create_backup(self.server, backup_kind=BACKUP_KIND_HOURLY)

        self.assertFalse(result["ok"])
        self.assertEqual(list(self.backup_dir.glob("*.tar.gz")), [])

    def test_backup_filename_uses_django_local_time(self):
        fixed_utc_now = datetime(2026, 8, 3, 0, 30, 45, tzinfo=ZoneInfo("UTC"))

        def _fake_archive(*args, **kwargs):
            Path(args[2]).write_bytes(b"backup")

        with mock.patch("apps.servers.backup_engine.timezone.now", return_value=fixed_utc_now), \
             mock.patch("apps.servers.backup_engine._archive_server_files", side_effect=_fake_archive):
            result = create_backup(self.server, backup_kind=BACKUP_KIND_HOURLY)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["filename"], "gtnh-production-20260803-023045-HOURLY.tar.gz")
        self.assertEqual(Path(result["path"]).parent, self.backup_dir / "hourly")

    def test_each_created_kind_uses_its_own_directory(self):
        def _fake_archive(*args, **kwargs):
            Path(args[2]).write_bytes(b"backup")

        with mock.patch("apps.servers.backup_engine._archive_server_files", side_effect=_fake_archive):
            for backup_kind, directory_name in (
                (BACKUP_KIND_HOURLY, "hourly"),
                (BACKUP_KIND_DAILY, "daily"),
                (BACKUP_KIND_WEEKLY, "weekly"),
                (BACKUP_KIND_MONTHLY, "monthly"),
                (BACKUP_KIND_USER, "user"),
            ):
                result = create_backup(self.server, backup_kind=backup_kind)
                self.assertTrue(result["ok"], result)
                self.assertEqual(Path(result["path"]).parent, self.backup_dir / directory_name)

    def test_rotation_result_contains_forensic_keep_and_delete_lists(self):
        for stamp in (
            "20260801-150450",
            "20260801-120123",
            "20260801-085758",
            "20260801-055435",
            "20260801-025112",
            "20260731-234750",
            "20260731-204425",
            "20260731-174102",
            "20260731-143735",
            "20260730-231858",
            "20260729-225145",
            "20260728-222406",
            "20260727-215816",
            "20260726-213226",
            "20260725-210626",
            "20260725-145959",
            "20260724-204011",
        ):
            (self.backup_dir / f"{self.server.slug}-{stamp}.tar.gz").write_bytes(b"backup")

        rotation = rotate_backups(self.server)

        self.assertIn(f"{self.server.slug}-20260801-150450.tar.gz", rotation["kept_files"])
        self.assertEqual(rotation["deleted_files"], [])
        self.assertEqual(rotation["rotated"], 0)
        self.assertEqual(rotation["kept_total"], 17)

    def test_rotation_does_not_delete_legacy_backups(self):
        stamps = [
            "20260810-215217",
            "20260811-221939",
            "20260812-225204",
            "20260813-110557",
            "20260813-140921",
            "20260813-171245",
            "20260813-201609",
            "20260813-231935",
            "20260814-022302",
            "20260814-052634",
            "20260814-083000",
            "20260814-111611",
            "20260814-142940",
        ]

        for stamp in stamps:
            (self.backup_dir / f"{self.server.slug}-{stamp}.tar.gz").write_bytes(b"backup")

        rotation = rotate_backups(self.server)
        kept = [item for item in list_backups(self.server) if item["protected_by_rotation"]]

        self.assertEqual(rotation["rotated"], 0)
        self.assertEqual(rotation["kept_total"], len(stamps))
        self.assertEqual(rotation["kept_legacy"], len(stamps))
        self.assertEqual(Counter(item["retention_bucket"] for item in kept), Counter({"legacy": len(stamps)}))
        self.assertIn(f"{self.server.slug}-20260813-110557.tar.gz", rotation["kept_files"])


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

    def _create_archive(self, dt, *, backup_kind=BACKUP_KIND_HOURLY, name=None):
        if name is None:
            name = f"{self.server.slug}-{dt.strftime('%Y%m%d-%H%M%S')}-{backup_kind}.tar.gz"
        archive = self.backup_dir / name
        archive.write_bytes(b"backup")
        return archive

    def test_no_archive_means_auto_backup_is_due(self):
        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["ok"])
        self.assertTrue(result["due"])

    def test_recent_auto_backup_is_not_due(self):
        hourly_dir = self.backup_dir / "hourly"
        hourly_dir.mkdir()
        name = f"{self.server.slug}-{(self.now - timedelta(hours=2, minutes=59)).strftime('%Y%m%d-%H%M%S')}-{BACKUP_KIND_HOURLY}.tar.gz"
        (hourly_dir / name).write_bytes(b"backup")

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["ok"])
        self.assertFalse(result["due"])
        self.assertEqual(result["backup_kind"], BACKUP_KIND_HOURLY)

    def test_auto_backup_exactly_three_hours_old_is_due(self):
        self._create_archive(self.now - timedelta(hours=AUTO_BACKUP_INTERVAL_HOURS))

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])

    def test_auto_backup_older_than_three_hours_is_due(self):
        self._create_archive(self.now - timedelta(hours=4))

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])

    def test_user_backup_does_not_delay_hourly_backup(self):
        self._create_archive(self.now - timedelta(hours=2), backup_kind=BACKUP_KIND_USER)

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])

    def test_daily_backup_is_due_once_after_two(self):
        before_two = self.now.replace(hour=1, minute=59)
        after_two = self.now.replace(hour=2, minute=0)

        self.assertNotIn(BACKUP_KIND_DAILY, check_backup_layers_due(self.server, now=before_two)["due_kinds"])
        self.assertIn(BACKUP_KIND_DAILY, check_backup_layers_due(self.server, now=after_two)["due_kinds"])

        self._create_archive(after_two, backup_kind=BACKUP_KIND_DAILY)
        self.assertNotIn(BACKUP_KIND_DAILY, check_backup_layers_due(self.server, now=self.now)["due_kinds"])

    def test_weekly_backup_is_due_after_seven_days_at_three(self):
        last_weekly = self.now - timedelta(days=7)
        self._create_archive(last_weekly, backup_kind=BACKUP_KIND_WEEKLY)

        before_three = self.now.replace(hour=2, minute=59)
        after_three = self.now.replace(hour=3, minute=0)
        self.assertNotIn(BACKUP_KIND_WEEKLY, check_backup_layers_due(self.server, now=before_three)["due_kinds"])
        self.assertIn(BACKUP_KIND_WEEKLY, check_backup_layers_due(self.server, now=after_three)["due_kinds"])

    def test_monthly_backup_is_due_only_last_day_after_four(self):
        last_day = timezone.make_aware(datetime(2026, 7, 31, 4, 0, 0), timezone.get_current_timezone())
        before_four = last_day.replace(hour=3, minute=59)
        previous_day = last_day - timedelta(days=1)

        self.assertNotIn(BACKUP_KIND_MONTHLY, check_backup_layers_due(self.server, now=previous_day)["due_kinds"])
        self.assertNotIn(BACKUP_KIND_MONTHLY, check_backup_layers_due(self.server, now=before_four)["due_kinds"])
        self.assertIn(BACKUP_KIND_MONTHLY, check_backup_layers_due(self.server, now=last_day)["due_kinds"])

        self._create_archive(last_day, backup_kind=BACKUP_KIND_MONTHLY)
        self.assertNotIn(BACKUP_KIND_MONTHLY, check_backup_layers_due(self.server, now=last_day.replace(hour=12))["due_kinds"])

    def test_other_auto_layers_do_not_delay_hourly_backup(self):
        self._create_archive(self.now - timedelta(minutes=10), backup_kind=BACKUP_KIND_DAILY)
        self._create_archive(self.now - timedelta(minutes=5), backup_kind=BACKUP_KIND_WEEKLY)

        result = check_auto_backup_due(self.server, now=self.now)

        self.assertTrue(result["due"])

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
            result = create_backup(self.server, backup_kind=BACKUP_KIND_HOURLY)

        self.assertFalse(result["ok"])
        self.assertEqual(list(self.backup_dir.glob("*.tar.gz")), [])

    def test_list_backups_uses_mtime_for_legacy_utc_named_archives(self):
        archive = self._create_archive(
            self.now - timedelta(hours=4),
            name=f"{self.server.slug}-20260803-064416.tar.gz",
        )
        actual_local_dt = timezone.make_aware(datetime(2026, 8, 3, 8, 44, 16), timezone.get_current_timezone())
        epoch = actual_local_dt.timestamp()
        os.utime(archive, (epoch, epoch))

        backups = list_backups(self.server)

        self.assertEqual(backups[0]["created_at_dt"], actual_local_dt)
        self.assertEqual(backups[0]["timestamp_source"], "mtime_legacy")

    def test_legacy_utc_named_archives_do_not_claim_extra_daily_bucket(self):
        raw = [
            ("20260804-065902", "local"),
            ("20260804-035545", "local"),
            ("20260804-005229", "local"),
            ("20260803-214910", "local"),
            ("20260803-184545", "local"),
            ("20260803-154219", "local"),
            ("20260803-123850", "local"),
            ("20260803-093835", "local"),
            ("20260803-064416", "utc_old"),
            ("20260802-213405", "utc_old"),
            ("20260802-182758", "utc_old_user"),
            ("20260801-212050", "utc_old"),
            ("20260731-234750", "utc_old"),
            ("20260730-231858", "utc_old"),
        ]
        prague = ZoneInfo("Europe/Prague")
        utc = ZoneInfo("UTC")

        for stamp, kind in raw:
            suffix = "-USER" if kind == "utc_old_user" else ""
            archive = self.backup_dir / f"{self.server.slug}-{stamp}{suffix}.tar.gz"
            archive.write_bytes(b"backup")
            naive = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            if kind.startswith("utc_old"):
                actual_dt = naive.replace(tzinfo=utc).astimezone(prague)
            else:
                actual_dt = naive.replace(tzinfo=prague)
            epoch = actual_dt.timestamp()
            os.utime(archive, (epoch, epoch))

        backups = list_backups(self.server)
        kept = [item for item in backups if item["protected_by_rotation"]]

        self.assertEqual(Counter(item["retention_bucket"] for item in kept), Counter({"legacy": 13, "user": 1}))
        self.assertEqual(len(kept), 14)


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

    def _create_files(self, directory: Path, slug: str, backup_kind: str, count: int) -> list[str]:
        names = []
        current = datetime(2026, 8, 16, 18, 0, 0)
        for index in range(count):
            stamp = (current - timedelta(hours=index)).strftime("%Y%m%d-%H%M%S")
            name = f"{slug}-{stamp}-{backup_kind}.tar.gz"
            (directory / name).write_bytes(b"x")
            names.append(name)
        return names

    def test_each_layer_rotates_only_its_own_files(self):
        with TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            server = self._make_server(backup_dir, backup_keep_count=12)
            hourly = self._create_files(backup_dir, server.slug, BACKUP_KIND_HOURLY, 10)
            daily = self._create_files(backup_dir, server.slug, BACKUP_KIND_DAILY, 9)
            weekly = self._create_files(backup_dir, server.slug, BACKUP_KIND_WEEKLY, 6)
            monthly = self._create_files(backup_dir, server.slug, BACKUP_KIND_MONTHLY, 14)

            rotation = rotate_backups(server)
            remaining = {item["name"] for item in list_backups(server)}

            self.assertEqual(rotation["rotated"], 8)
            self.assertEqual(rotation["kept_hourly"], 8)
            self.assertEqual(rotation["kept_daily"], 7)
            self.assertEqual(rotation["kept_weekly"], 4)
            self.assertEqual(rotation["kept_monthly"], 12)
            self.assertEqual(remaining, set(hourly[:8] + daily[:7] + weekly[:4] + monthly[:12]))

    def test_layer_scoped_rotation_leaves_other_layer_excess_untouched(self):
        with TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            server = self._make_server(backup_dir)
            hourly = self._create_files(backup_dir, server.slug, BACKUP_KIND_HOURLY, 10)
            daily = self._create_files(backup_dir, server.slug, BACKUP_KIND_DAILY, 9)

            rotation = rotate_backups(server, backup_kind=BACKUP_KIND_HOURLY)
            remaining = {item["name"] for item in list_backups(server)}

            self.assertEqual(rotation["deleted_files"], hourly[8:])
            self.assertTrue(set(daily).issubset(remaining))
            self.assertEqual(len([name for name in remaining if name.endswith("-DAILY.tar.gz")]), 9)

    def test_user_and_legacy_backups_are_never_deleted(self):
        with TemporaryDirectory() as temp_dir:
            backup_dir = Path(temp_dir)
            server = self._make_server(backup_dir)
            user_names = self._create_files(backup_dir, server.slug, BACKUP_KIND_USER, 15)
            legacy_names = []
            for index in range(15):
                name = f"{server.slug}-202607{index + 1:02d}-120000.tar.gz"
                (backup_dir / name).write_bytes(b"x")
                legacy_names.append(name)

            rotation = rotate_backups(server)
            remaining = {item["name"] for item in list_backups(server)}

            self.assertEqual(rotation["rotated"], 0)
            self.assertEqual(rotation["kept_user"], 15)
            self.assertEqual(rotation["kept_legacy"], 15)
            self.assertEqual(remaining, set(user_names + legacy_names))
