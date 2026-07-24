from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import psutil
from django.test import SimpleTestCase, TestCase

from apps.audit.models import AuditEvent
from apps.control.backends.tmux import LocalTmuxProcessBackend
from apps.control.management.commands.run_runtime_worker import _check_backup
from apps.servers.models import GameType
from apps.servers.models import Server, ServerStatus


class FakeProcess:
    def __init__(
        self,
        pid: int,
        name: str,
        *,
        rss: int = 0,
        threads: int = 1,
        running: bool = True,
        children: list[FakeProcess] | None = None,
        cpu_samples: list[float] | None = None,
        create_time: float = 1.0,
    ) -> None:
        self.pid = pid
        self._name = name
        self._rss = rss
        self._threads = threads
        self._running = running
        self._children = list(children or [])
        self._cpu_samples = list(cpu_samples or [0.0])
        self._create_time = create_time
        self.cpu_calls = 0

    def is_running(self) -> bool:
        return self._running

    def name(self) -> str:
        return self._name

    def children(self, recursive: bool = False) -> list[FakeProcess]:
        if not recursive:
            return list(self._children)
        descendants: list[FakeProcess] = []
        stack = list(self._children)
        while stack:
            current = stack.pop(0)
            descendants.append(current)
            stack.extend(current.children(recursive=False))
        return descendants

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)

    def num_threads(self) -> int:
        return self._threads

    def cpu_percent(self, interval=None) -> float:
        index = min(self.cpu_calls, len(self._cpu_samples) - 1)
        self.cpu_calls += 1
        return self._cpu_samples[index]

    def create_time(self) -> float:
        return self._create_time


class LocalTmuxProcessBackendTests(SimpleTestCase):
    def make_server(self, **overrides) -> Server:
        params = {
            "name": "Test",
            "slug": "test",
            "game_type": "minecraft_java",
            "working_directory": "/srv/server",
            "start_command": "java -jar server.jar nogui",
            "stop_command": "stop",
            "tmux_session_name": "srv-test",
            "pid_file_path": "",
        }
        params.update(overrides)
        return Server(**params)

    def test_find_pid_prefers_java_pane_over_tee_child(self):
        backend = LocalTmuxProcessBackend()
        server = self.make_server()
        tee_proc = FakeProcess(3465458, "tee", rss=2 * 1024 * 1024)
        java_proc = FakeProcess(3465454, "java", rss=7 * 1024 * 1024 * 1024, threads=71, children=[tee_proc])

        with mock.patch.object(backend, "_run", return_value=mock.Mock(returncode=0, stdout="3465454\n")), \
             mock.patch("apps.control.backends.tmux.psutil.Process", return_value=java_proc):
            self.assertEqual(backend._find_pid(server), 3465454)

    def test_find_pid_prefers_java_child_when_pane_is_shell(self):
        backend = LocalTmuxProcessBackend()
        server = self.make_server()
        java_proc = FakeProcess(3465454, "java", rss=7 * 1024 * 1024 * 1024, threads=71)
        tee_proc = FakeProcess(3465458, "tee", rss=2 * 1024 * 1024)
        shell_proc = FakeProcess(3465400, "bash", children=[java_proc, tee_proc])

        with mock.patch.object(backend, "_run", return_value=mock.Mock(returncode=0, stdout="3465400\n")), \
             mock.patch("apps.control.backends.tmux.psutil.Process", return_value=shell_proc):
            self.assertEqual(backend._find_pid(server), 3465454)

    def test_find_pid_uses_valid_pid_file_first(self):
        backend = LocalTmuxProcessBackend()
        with TemporaryDirectory() as temp_dir:
            pid_file = Path(temp_dir) / "server.pid"
            pid_file.write_text("555\n", encoding="utf-8")
            server = self.make_server(pid_file_path=str(pid_file))

            with mock.patch("apps.control.backends.tmux.psutil.pid_exists", return_value=True), \
                 mock.patch.object(backend, "_run") as run_mock:
                self.assertEqual(backend._find_pid(server), 555)
                run_mock.assert_not_called()

    def test_find_pid_falls_back_when_pid_file_is_stale(self):
        backend = LocalTmuxProcessBackend()
        with TemporaryDirectory() as temp_dir:
            pid_file = Path(temp_dir) / "server.pid"
            pid_file.write_text("555\n", encoding="utf-8")
            server = self.make_server(pid_file_path=str(pid_file))
            java_proc = FakeProcess(777, "java", rss=1024)

            with mock.patch("apps.control.backends.tmux.psutil.pid_exists", return_value=False), \
                 mock.patch.object(backend, "_run", return_value=mock.Mock(returncode=0, stdout="777\n")), \
                 mock.patch("apps.control.backends.tmux.psutil.Process", return_value=java_proc):
                self.assertEqual(backend._find_pid(server), 777)

    def test_get_process_info_handles_vanishing_process(self):
        backend = LocalTmuxProcessBackend()
        server = self.make_server()

        with mock.patch.object(backend, "session_exists", return_value=True), \
             mock.patch.object(backend, "_find_pid", return_value=999), \
             mock.patch("apps.control.backends.tmux.psutil.Process", side_effect=psutil.NoSuchProcess(999)):
            info = backend.get_process_info(server)

        self.assertIsNone(info.pid)
        self.assertEqual(info.status, ServerStatus.CRASHED)
        self.assertTrue(info.tmux_alive)

    def test_get_process_info_reuses_cached_cpu_sampler_for_same_pid(self):
        backend = LocalTmuxProcessBackend()
        server = self.make_server()
        proc = FakeProcess(3465454, "java", rss=7, threads=71, cpu_samples=[0.0, 136.0], create_time=100.0)

        with mock.patch.object(backend, "session_exists", return_value=True), \
             mock.patch.object(backend, "_find_pid", return_value=3465454), \
             mock.patch("apps.control.backends.tmux.psutil.Process", return_value=proc) as process_mock:
            first = backend.get_process_info(server)
            second = backend.get_process_info(server)

        self.assertEqual(first.cpu_percent, 0.0)
        self.assertEqual(second.cpu_percent, 136.0)
        self.assertEqual(process_mock.call_count, 2)
        self.assertEqual(proc.cpu_calls, 2)

    def test_get_process_info_resets_cpu_cache_when_pid_changes(self):
        backend = LocalTmuxProcessBackend()
        server = self.make_server()
        first_proc = FakeProcess(100, "java", rss=1, cpu_samples=[0.0, 55.0], create_time=10.0)
        second_proc = FakeProcess(200, "java", rss=1, cpu_samples=[0.0, 88.0], create_time=20.0)

        with mock.patch.object(backend, "session_exists", return_value=True), \
             mock.patch.object(backend, "_find_pid", side_effect=[100, 200, 200]), \
             mock.patch("apps.control.backends.tmux.psutil.Process", side_effect=[first_proc, second_proc, second_proc]):
            first = backend.get_process_info(server)
            second = backend.get_process_info(server)
            third = backend.get_process_info(server)

        self.assertEqual(first.cpu_percent, 0.0)
        self.assertEqual(second.cpu_percent, 0.0)
        self.assertEqual(third.cpu_percent, 88.0)
        self.assertNotIn(100, backend._cpu_samples)

    def test_get_process_info_handles_access_denied(self):
        backend = LocalTmuxProcessBackend()
        server = self.make_server()

        proc = mock.Mock()
        proc.pid = 321
        proc.is_running.return_value = True
        proc.cpu_percent.side_effect = psutil.AccessDenied()

        with mock.patch.object(backend, "session_exists", return_value=True), \
             mock.patch.object(backend, "_find_pid", return_value=321), \
             mock.patch("apps.control.backends.tmux.psutil.Process", return_value=proc):
            info = backend.get_process_info(server)

        self.assertEqual(info.pid, 321)
        self.assertEqual(info.status, ServerStatus.ONLINE)
        self.assertEqual(info.cpu_percent, 0.0)
        self.assertEqual(info.rss_bytes, 0)
        self.assertEqual(info.thread_count, 0)


class WorkerAutoBackupTests(TestCase):
    def make_server(self, **overrides) -> Server:
        params = {
            "name": "Test",
            "slug": "test",
            "game_type": GameType.MINECRAFT_JAVA,
            "working_directory": "/srv/server",
            "start_command": "java -jar server.jar nogui",
            "stop_command": "stop",
            "tmux_session_name": "srv_test",
            "log_file_path": "/srv/server/logs/latest.log",
            "backup_directory": "/srv/server/backups",
            "backup_max_age_hours": 24,
            "status": ServerStatus.OFFLINE,
        }
        params.update(overrides)
        return Server.objects.create(**params)

    def test_check_backup_calls_auto_backup_only_when_due(self):
        server = self.make_server()
        due_result = {
            "ok": True,
            "due": True,
            "interval_hours": 3,
            "message": "Nenalezena žádná panelová záloha; vytvářím první AUTO zálohu.",
        }

        with mock.patch("apps.servers.backup.check_auto_backup_due", return_value=due_result), \
             mock.patch("apps.servers.backup_engine.create_backup", return_value={"ok": True, "message": "created"}) as create_backup_mock, \
             self.assertLogs("apps.control.management.commands.run_runtime_worker", level="INFO") as logs:
            _check_backup(server)

        create_backup_mock.assert_called_once_with(server, is_user=False)
        self.assertTrue(any("AUTO backup je splatný" in entry for entry in logs.output))

    def test_check_backup_skips_auto_backup_when_not_due(self):
        server = self.make_server()
        due_result = {
            "ok": True,
            "due": False,
            "age_hours": 1.5,
            "interval_hours": 3,
            "newest_file": "test-20260724-103000-USER.tar.gz",
            "newest_kind": "USER",
            "message": "Poslední panelová záloha test-20260724-103000-USER.tar.gz (USER) je stará 1.5h; AUTO záloha zatím není splatná před 3h intervalem.",
        }

        with mock.patch("apps.servers.backup.check_auto_backup_due", return_value=due_result), \
             mock.patch("apps.servers.backup_engine.create_backup") as create_backup_mock, \
             self.assertLogs("apps.control.management.commands.run_runtime_worker", level="INFO") as logs:
            _check_backup(server)

        create_backup_mock.assert_not_called()
        self.assertTrue(any("AUTO backup zatím není splatný" in entry for entry in logs.output))

    def test_check_backup_logs_and_audits_create_failure_without_crashing(self):
        server = self.make_server()
        due_result = {
            "ok": True,
            "due": True,
            "age_hours": 4.0,
            "interval_hours": 3,
            "newest_file": "test-20260724-080000.tar.gz",
            "newest_kind": "AUTO",
            "message": "Poslední panelová záloha test-20260724-080000.tar.gz (AUTO) je stará 4.0h; překročen 3h interval, AUTO záloha je splatná.",
        }
        backup_result = {"ok": False, "message": "disk full"}

        with mock.patch("apps.servers.backup.check_auto_backup_due", return_value=due_result), \
             mock.patch("apps.servers.backup_engine.create_backup", return_value=backup_result), \
             self.assertLogs("apps.control.management.commands.run_runtime_worker", level="ERROR") as logs:
            _check_backup(server)

        event = AuditEvent.objects.get(server=server, event_type="server.backup.auto_failed")
        self.assertEqual(event.message, "disk full")
        self.assertEqual(event.payload_json["due"]["newest_file"], "test-20260724-080000.tar.gz")
        self.assertEqual(event.payload_json["auto_backup"]["message"], "disk full")
        self.assertTrue(any("AUTO backup selhal: disk full" in entry for entry in logs.output))