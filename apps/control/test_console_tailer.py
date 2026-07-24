from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from django.db.utils import InterfaceError
from django.test import SimpleTestCase, TestCase

from apps.console.buffer import clear_console_lines, get_console_lines
from apps.console.consumers import ServerConsumer
from apps.control.management.commands.run_runtime_worker import (
    _ConsoleBroadcastWorker,
    _LogTailer,
    _ConsoleReadResult,
    _build_console_ws_messages,
    _process_console_read,
    _run_worker_thread,
    start_runtime_threads,
)
from apps.servers.models import Server, ServerProcessState, ServerStatus


class _FakePublisher:
    def __init__(self, *, should_raise: bool = False):
        self.should_raise = should_raise
        self.calls: list[dict] = []

    def enqueue(self, server_id: str, stored_entries: list[dict], *, replace=False):
        built_messages = _build_console_ws_messages(server_id, stored_entries, replace=replace)
        self.calls.append({
            "server_id": server_id,
            "stored_entries": stored_entries,
            "replace": replace,
            "messages": built_messages,
        })
        if self.should_raise:
            raise RuntimeError("group_send timeout")
        return True


class ConsoleTailerTests(SimpleTestCase):
    def make_server(self, log_path: str, *, server_id: str = "srv-1"):
        return SimpleNamespace(
            id=server_id,
            slug="test",
            log_file_path=log_path,
            status=ServerStatus.ONLINE,
        )

    def tearDown(self):
        clear_console_lines("srv-1")
        clear_console_lines("srv-2")
        clear_console_lines("srv-3")
        clear_console_lines("srv-4")
        clear_console_lines("srv-5")
        super().tearDown()

    def test_initial_open_seeds_context_without_live_side_effects(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "panel-console.log"
            log_path.write_text("old-1\nold-2\n", encoding="utf-8")
            server = self.make_server(str(log_path))
            tailer = _LogTailer(server)

            try:
                console_read = tailer.read_new_lines()
                publisher = _FakePublisher()

                with mock.patch("apps.control.management.commands.run_runtime_worker.touch_console_activity") as touch_mock, \
                     mock.patch("apps.control.management.commands.run_runtime_worker._check_startup") as startup_mock, \
                     mock.patch("apps.servers.player_tracker.process_line_for_players") as tracker_mock, \
                     mock.patch("apps.alerts.engine.check_log_pattern_alert") as alert_mock:
                    _process_console_read(server, console_read, publisher)

                self.assertFalse(console_read.is_live)
                self.assertTrue(console_read.replace_buffer)
                self.assertEqual([item["line"] for item in get_console_lines(server.id)], ["old-1", "old-2"])
                self.assertTrue(all(item["is_live"] is False for item in get_console_lines(server.id)))
                touch_mock.assert_not_called()
                startup_mock.assert_not_called()
                tracker_mock.assert_not_called()
                alert_mock.assert_not_called()
                self.assertEqual(len(publisher.calls), 1)
                self.assertTrue(publisher.calls[0]["replace"])
            finally:
                tailer.close()

    def test_live_append_is_delivered_once_and_not_duplicated_on_idle_poll(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "panel-console.log"
            log_path.write_text("snapshot\n", encoding="utf-8")
            server = self.make_server(str(log_path), server_id="srv-2")
            tailer = _LogTailer(server)

            try:
                _process_console_read(server, tailer.read_new_lines(), _FakePublisher())

                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("live-1\n")

                publisher = _FakePublisher()
                with mock.patch("apps.control.management.commands.run_runtime_worker.touch_console_activity") as touch_mock, \
                     mock.patch("apps.control.management.commands.run_runtime_worker._check_startup") as startup_mock, \
                     mock.patch("apps.servers.player_tracker.process_line_for_players") as tracker_mock, \
                     mock.patch("apps.alerts.engine.check_log_pattern_alert") as alert_mock:
                    console_read = tailer.read_new_lines()
                    _process_console_read(server, console_read, publisher)

                self.assertTrue(console_read.is_live)
                self.assertEqual(console_read.lines, ["live-1"])
                self.assertEqual(tailer.read_new_lines(), None)
                self.assertEqual(get_console_lines(server.id)[-1]["line"], "live-1")
                self.assertTrue(get_console_lines(server.id)[-1]["is_live"])
                touch_mock.assert_called_once()
                startup_mock.assert_called_once_with(server, ["live-1"])
                tracker_mock.assert_called_once_with(server, "live-1")
                alert_mock.assert_called_once_with(server, "live-1")
                self.assertEqual(len(publisher.calls), 1)
                self.assertFalse(publisher.calls[0]["replace"])
            finally:
                tailer.close()

    def test_inode_replacement_reopens_new_file_and_resets_generation(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "panel-console.log"
            log_path.write_text("old-1\nold-2\n", encoding="utf-8")
            server = self.make_server(str(log_path), server_id="srv-3")
            tailer = _LogTailer(server)

            try:
                _process_console_read(server, tailer.read_new_lines(), _FakePublisher())
                log_path.write_text("new-1\nnew-2\n", encoding="utf-8")

                original_stat = log_path.stat()
                fake_stat = SimpleNamespace(st_ino=(tailer._inode or 1) + 1, st_size=original_stat.st_size)
                publisher = _FakePublisher()
                with mock.patch("pathlib.Path.stat", autospec=True, return_value=fake_stat):
                    console_read = tailer.read_new_lines()
                    _process_console_read(server, console_read, publisher)

                self.assertFalse(console_read.is_live)
                self.assertEqual([item["line"] for item in get_console_lines(server.id)], ["new-1", "new-2"])
                self.assertTrue(publisher.calls[0]["replace"])
            finally:
                tailer.close()

    def test_same_inode_truncation_replaces_old_generation(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "panel-console.log"
            log_path.write_text("old-1\nold-2\nold-3\n", encoding="utf-8")
            server = self.make_server(str(log_path), server_id="srv-4")
            tailer = _LogTailer(server)

            try:
                _process_console_read(server, tailer.read_new_lines(), _FakePublisher())
                log_path.write_text("fresh-1\n", encoding="utf-8")

                console_read = tailer.read_new_lines()
                _process_console_read(server, console_read, _FakePublisher())

                self.assertFalse(console_read.is_live)
                self.assertEqual([item["line"] for item in get_console_lines(server.id)], ["fresh-1"])
            finally:
                tailer.close()

    def test_empty_truncation_clears_buffer_and_emits_empty_replace_event(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "panel-console.log"
            log_path.write_text("old-1\nold-2\n", encoding="utf-8")
            server = self.make_server(str(log_path), server_id="srv-5")
            tailer = _LogTailer(server)

            try:
                _process_console_read(server, tailer.read_new_lines(), _FakePublisher())
                log_path.write_text("", encoding="utf-8")

                publisher = _FakePublisher()
                console_read = tailer.read_new_lines()
                _process_console_read(server, console_read, publisher)

                self.assertEqual(get_console_lines(server.id), [])
                self.assertEqual(len(publisher.calls), 1)
                self.assertTrue(publisher.calls[0]["replace"])
                self.assertEqual(len(publisher.calls[0]["messages"]), 1)
                self.assertEqual(publisher.calls[0]["messages"][0].payload, {
                    "type": "console.lines",
                    "replace": True,
                    "lines": [],
                })
            finally:
                tailer.close()

    def test_large_console_batch_is_chunked_for_websocket_delivery(self):
        stored_entries = [
            {
                "timestamp": "2026-07-24T06:43:03+02:00",
                "line": f"line-{index}",
                "is_live": True,
            }
            for index in range(205)
        ]

        messages = _build_console_ws_messages("srv-1", stored_entries, batch_size=100)

        self.assertEqual(len(messages), 3)
        self.assertEqual([len(item.payload["lines"]) for item in messages], [100, 100, 5])

    def test_broadcast_worker_drops_full_replacement_update_atomically(self):
        worker = _ConsoleBroadcastWorker(channel_layer=mock.Mock(), max_queue_size=1)
        existing_entries = [{"timestamp": "t0", "line": "existing", "is_live": True}]
        replacement_entries = [
            {"timestamp": "t1", "line": f"line-{index}", "is_live": False}
            for index in range(205)
        ]

        self.assertTrue(worker.enqueue("srv-1", existing_entries, replace=False))
        self.assertFalse(worker.enqueue("srv-1", replacement_entries, replace=True))
        self.assertEqual(worker._queue.qsize(), 1)

        queued_update = worker._queue.get_nowait()
        self.assertFalse(queued_update.replace)
        self.assertEqual(queued_update.stored_entries, existing_entries)
        self.assertTrue(worker._queue.empty())

    def test_disconnect_discards_all_groups_even_if_one_fails(self):
        consumer = ServerConsumer()
        consumer.groups = ["g1", "g2", "g3"]
        consumer.channel_name = "chan-1"
        consumer.channel_layer = SimpleNamespace(
            group_discard=mock.AsyncMock(side_effect=[RuntimeError("boom"), None, None])
        )
        consumer.user = "tester"
        consumer.slug = "test"

        async_to_sync(consumer.disconnect)(1000)

        self.assertEqual(consumer.channel_layer.group_discard.await_count, 3)

    def test_run_worker_thread_closes_connections_quietly_on_closed_db(self):
        stop_event = mock.Mock()
        target = mock.Mock()

        with mock.patch("apps.control.management.commands.run_runtime_worker.close_old_connections") as close_old_mock, \
             mock.patch("apps.control.management.commands.run_runtime_worker.connections.close_all", side_effect=InterfaceError("closed")):
            _run_worker_thread(target, stop_event)

        close_old_mock.assert_called_once()
        target.assert_called_once_with(stop_event)

    def test_start_runtime_threads_uses_daemon_workers(self):
        created_threads = []

        class _FakeThread:
            def __init__(self, *args, **kwargs):
                self.target = kwargs.get("target")
                self.args = kwargs.get("args")
                self.name = kwargs.get("name")
                self.daemon = kwargs.get("daemon")
                self.started = False
                created_threads.append(self)

            def start(self):
                self.started = True

        stop_event = mock.Mock()

        with mock.patch("apps.control.management.commands.run_runtime_worker.threading.Thread", side_effect=_FakeThread):
            returned_event, threads = start_runtime_threads(stop_event)

        self.assertIs(returned_event, stop_event)
        self.assertEqual(len(threads), 4)
        self.assertEqual(threads, created_threads)
        self.assertTrue(all(thread.daemon is True for thread in threads))
        self.assertTrue(all(thread.started for thread in threads))


class ConsoleTailerDbTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(
            name="Test",
            slug="test-db",
            game_type="minecraft_java",
            working_directory="/tmp/test-db",
            start_command="java -jar server.jar nogui",
            stop_command="stop",
            tmux_session_name="test-db",
            log_file_path="/tmp/test-db/logs/panel-console.log",
            status=ServerStatus.ONLINE,
        )
        ServerProcessState.objects.create(server=self.server, status=ServerStatus.ONLINE)

    def tearDown(self):
        clear_console_lines(self.server.id)
        super().tearDown()

    def test_ws_enqueue_failure_does_not_block_ingestion_or_last_log_update(self):
        console_read = _ConsoleReadResult(
            lines=["live-1"],
            is_live=True,
            replace_buffer=False,
            source="log_tail",
        )

        with mock.patch("apps.control.management.commands.run_runtime_worker._check_startup") as startup_mock, \
             mock.patch("apps.servers.player_tracker.process_line_for_players") as tracker_mock, \
             mock.patch("apps.alerts.engine.check_log_pattern_alert") as alert_mock:
            _process_console_read(self.server, console_read, _FakePublisher(should_raise=True))

        self.server.process_state.refresh_from_db()
        self.assertIsNotNone(self.server.process_state.last_log_line_at)
        self.assertEqual(get_console_lines(self.server.id)[-1]["line"], "live-1")
        startup_mock.assert_called_once_with(self.server, ["live-1"])
        tracker_mock.assert_called_once_with(self.server, "live-1")
        alert_mock.assert_called_once_with(self.server, "live-1")