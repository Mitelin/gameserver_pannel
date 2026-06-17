"""
apps/control/backends/subprocess_backend.py

SubprocessBackend – spouští servery přímo přes subprocess.Popen.

Web konzole čte stdout/stderr přímo z procesu.
Poslední řádky držíme v cache/RAM bufferu pro replay po znovuotevření stránky.
"""
import os
import sys
import shlex
import logging
import subprocess
import threading
import queue
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import psutil

from apps.console.buffer import append_console_lines, clear_console_lines, touch_console_activity
from apps.control.startup_probe import is_startup_ready
from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)

# slug → Popen
_processes: dict[str, subprocess.Popen] = {}

# slug → direct process console relay
_console_relays: dict[str, "_ProcessConsoleRelay"] = {}

# Cache psutil.Process objektů — nutné pro správné CPU měření
_proc_cache: dict[int, psutil.Process] = {}


def _windows_creation_flags() -> int:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _resolve_start_command(server: Server) -> str:
    if server.start_command.strip():
        return server.start_command.strip()
    try:
        profile = server.start_profiles.filter(is_active=True).first()
        if profile:
            return profile.build_command()
    except Exception:
        pass
    return ""


# ── Direct process relay: čte stdout/stderr a posílá do WS + RAM bufferu ─────
class _ProcessConsoleRelay:
    MAX_BATCH_SIZE = 200
    IDLE_WAIT = 0.2

    def __init__(self, server: Server, proc: subprocess.Popen):
        self.server_pk = server.pk
        self.server_id = str(server.id)
        self.slug = server.slug
        self.game_type = server.game_type
        self.proc = proc
        self.group = f"server.{self.server_id}.console"
        self.stop_event = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self.reader_count = 0
        self.reader_done = 0
        self.threads: list[threading.Thread] = []

    def start(self):
        streams = [
            (self.proc.stdout, "stdout"),
            (self.proc.stderr, "stderr"),
        ]
        self.reader_count = sum(1 for stream, _ in streams if stream is not None)
        for stream, stream_type in streams:
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._read_stream,
                args=(stream, stream_type),
                daemon=True,
                name=f"console-reader-{self.slug}-{stream_type}",
            )
            thread.start()
            self.threads.append(thread)

        publisher = threading.Thread(
            target=self._publish_loop,
            daemon=True,
            name=f"console-publisher-{self.slug}",
        )
        publisher.start()
        self.threads.append(publisher)
        logger.info("[%s] direct console relay spuštěn", self.slug)

    def is_alive(self) -> bool:
        return any(thread.is_alive() for thread in self.threads)

    def stop(self):
        self.stop_event.set()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    def _read_stream(self, stream, stream_type: str):
        try:
            while not self.stop_event.is_set():
                line = stream.readline()
                if line == "":
                    break
                self.queue.put((stream_type, line.rstrip("\r\n")))
        except Exception as exc:
            logger.debug("[%s] console %s reader: %s", self.slug, stream_type, exc)
        finally:
            self.queue.put((None, stream_type))

    def _publish_loop(self):
        while True:
            try:
                item = self.queue.get(timeout=self.IDLE_WAIT)
            except queue.Empty:
                item = None

            if item is not None:
                batch: list[tuple[str, str]] = []
                self._consume_queue_item(item, batch)
                while len(batch) < self.MAX_BATCH_SIZE:
                    try:
                        queued_item = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    self._consume_queue_item(queued_item, batch)
                if batch:
                    self._flush_batch(batch)

            if self.reader_done >= self.reader_count and self.queue.empty():
                break

            if self.stop_event.is_set() and self.queue.empty() and self.proc.poll() is not None:
                break

        _console_relays.pop(self.slug, None)
        logger.info("[%s] direct console relay ukončen", self.slug)

    def _consume_queue_item(self, item: tuple[str | None, str], batch: list[tuple[str, str]]):
        stream_type, payload = item
        if stream_type is None:
            self.reader_done += 1
            return
        batch.append((stream_type, payload))

    def _flush_batch(self, batch: list[tuple[str, str]]):
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from django.utils import timezone
        from apps.audit.models import AuditEvent
        from apps.servers.models import Server as Srv, ServerStatus as Status

        if not batch:
            return

        channel_layer = get_channel_layer()
        now = timezone.now()
        lines_only = [line for _, line in batch]

        # Live UI má přednost před persistencí do DB.
        for _, line in batch:
            try:
                async_to_sync(channel_layer.group_send)(
                    self.group,
                    {
                        "type": "console.line",
                        "server_id": self.server_id,
                        "timestamp": now.isoformat(),
                        "line": line,
                    },
                )
            except Exception as exc:
                logger.debug("[%s] WS push: %s", self.slug, exc)

        append_console_lines(self.server_pk, batch, source="subprocess")

        server = Srv.objects.select_related("process_state").get(pk=self.server_pk)

        try:
            touch_console_activity(server, now)
        except Exception:
            pass

        for line in lines_only:
            try:
                from apps.servers.player_tracker import process_line_for_players
                process_line_for_players(server, line)
            except Exception as exc:
                logger.debug("player_tracker chyba: %s", exc)

            try:
                from apps.alerts.engine import check_log_pattern_alert
                check_log_pattern_alert(server, line)
            except Exception as exc:
                logger.debug("alert engine chyba: %s", exc)

        if server.status == Status.STARTING:
            for line in lines_only:
                if is_startup_ready(server):
                    logger.info("[%s] Startup confirmed: %s", self.slug, line[:80])
                    server.status = Status.ONLINE
                    server.save(update_fields=["status", "updated_at"])
                    try:
                        state = server.process_state
                        state.status = Status.ONLINE
                        state.consecutive_failures = 0
                        state.save(update_fields=["status", "consecutive_failures"])
                    except Exception:
                        pass
                    AuditEvent.objects.create(
                        server=server,
                        event_type="server.start.confirmed",
                        severity="info",
                        message=f"Startup ready: {line[:100]}",
                    )
                    break


def ensure_console_capture(server: Server, proc: subprocess.Popen | None = None) -> bool:
    relay = _console_relays.get(server.slug)
    if relay and relay.is_alive():
        return True

    if relay:
        relay.stop()
        _console_relays.pop(server.slug, None)

    if proc is None:
        proc = _processes.get(server.slug)
    if proc is None or proc.poll() is not None:
        return False

    relay = _ProcessConsoleRelay(server, proc)
    _console_relays[server.slug] = relay
    relay.start()
    return True


def stop_console_capture(slug: str):
    relay = _console_relays.pop(slug, None)
    if relay:
        relay.stop()


@dataclass
class ProcessInfo:
    pid: Optional[int]
    status: str
    cpu_percent: float
    rss_bytes: int
    thread_count: int
    tmux_alive: bool


class SubprocessError(Exception):
    pass


class SubprocessBackend:

    requires_terminal_session = False

    def _pid_file(self, server: Server) -> Path:
        return Path(server.working_directory) / ".panel_pid"

    def _save_pid(self, server: Server, pid: int):
        try:
            self._pid_file(server).write_text(str(pid))
        except Exception as exc:
            logger.warning("Nelze uložit PID soubor pro %s: %s", server.slug, exc)

    def _clear_pid(self, server: Server):
        try:
            self._pid_file(server).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Nelze smazat PID soubor pro %s: %s", server.slug, exc)

    def _load_pid(self, server: Server) -> Optional[int]:
        proc = _processes.get(server.slug)
        if proc is not None:
            if proc.poll() is None:
                return proc.pid
            _processes.pop(server.slug, None)
        try:
            return int(self._pid_file(server).read_text().strip())
        except (OSError, ValueError):
            return None

    def session_exists(self, server: Server) -> bool:
        pid = self._load_pid(server)
        if pid is None:
            return False
        try:
            return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            return False

    # ── Veřejné API ──────────────────────────────────────────────────────────

    def start_server(self, server: Server) -> None:
        if self.session_exists(server):
            raise SubprocessError(f"Server '{server.name}' již běží (PID {self._load_pid(server)}).")

        workdir = server.working_directory
        if not os.path.isdir(workdir):
            raise SubprocessError(f"Pracovní adresář neexistuje: {workdir}")

        start_cmd = _resolve_start_command(server)
        if not start_cmd:
            raise SubprocessError("Žádný start command ani aktivní profil.")

        logger.info("[%s] Spouštím '%s' v %s", server.slug, start_cmd, workdir)
        try:
            clear_console_lines(server.id)
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    start_cmd,
                    cwd=workdir,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=_windows_creation_flags(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            else:
                proc = subprocess.Popen(
                    shlex.split(start_cmd),
                    cwd=workdir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
        except Exception as exc:
            raise SubprocessError(f"Nelze spustit server: {exc}") from exc

        _processes[server.slug] = proc
        self._save_pid(server, proc.pid)

        ensure_console_capture(server, proc)

        logger.info("[%s] Spuštěn PID %d", server.slug, proc.pid)

    def stop_server(self, server: Server) -> None:
        stop_cmd = (server.stop_command or "stop").strip()
        proc = _processes.get(server.slug)
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(stop_cmd + "\n")
                proc.stdin.flush()
                return
            except Exception as exc:
                logger.warning("[%s] Nelze zapsat do stdin: %s", server.slug, exc)
        pid = self._load_pid(server)
        if pid:
            self._kill_tree(pid, "terminate")

    def kill_server(self, server: Server) -> None:
        proc = _processes.pop(server.slug, None)
        pid = None
        if proc is not None:
            try:
                pid = proc.pid
            except Exception:
                pid = None
        if pid is None:
            pid = self._load_pid(server)
        if pid:
            self._kill_tree(pid, "kill")
        stop_console_capture(server.slug)
        self._clear_pid(server)

    def _kill_tree(self, pid: int, sig: str = "terminate") -> None:
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for ch in children:
                try:
                    getattr(ch, sig)()
                except psutil.NoSuchProcess:
                    pass
            try:
                getattr(parent, sig)()
            except psutil.NoSuchProcess:
                pass
        except psutil.NoSuchProcess:
            pass

    def send_command(self, server: Server, command: str) -> None:
        proc = _processes.get(server.slug)
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
                return
            except Exception as exc:
                raise SubprocessError(f"Nelze odeslat příkaz: {exc}") from exc
        raise SubprocessError("Server není spuštěný nebo nemá stdin handle (restartuj server z panelu).")

    def get_console_state(self, server: Server) -> dict:
        proc = _processes.get(server.slug)
        relay = _console_relays.get(server.slug)

        if proc is not None and proc.poll() is None and relay and relay.is_alive():
            return {"available": True, "can_write": True, "message": "Konzole připojena."}

        pid = self._load_pid(server)
        if pid is not None:
            if server.rcon_enabled and server.rcon_password:
                return {
                    "available": False,
                    "can_write": True,
                    "message": "Live stdout po restartu panelu není připojený. Příkazy lze dál posílat přes RCON; zobrazuji poslední log.",
                }
            return {
                "available": False,
                "can_write": False,
                "message": "Live stdout po restartu panelu není připojený. Zobrazuji poslední log; příkazy bez RCON vyžadují restart serveru z panelu.",
            }

        return {"available": False, "can_write": False, "message": "Server je offline."}

    def get_process_info(self, server: Server) -> ProcessInfo:
        proc = _processes.get(server.slug)
        if proc is not None:
            returncode = proc.poll()
            if returncode is not None:
                _processes.pop(server.slug, None)
                stop_console_capture(server.slug)
                self._clear_pid(server)
                status = ServerStatus.OFFLINE if returncode == 0 else ServerStatus.CRASHED
                return ProcessInfo(pid=None, status=status,
                                   cpu_percent=0.0, rss_bytes=0, thread_count=0, tmux_alive=False)

        pid = self._load_pid(server)
        if pid is None:
            return ProcessInfo(pid=None, status=ServerStatus.OFFLINE,
                               cpu_percent=0.0, rss_bytes=0, thread_count=0, tmux_alive=False)
        try:
            parent = psutil.Process(pid)
            if not parent.is_running():
                raise psutil.NoSuchProcess(pid)

            # Získej celý process tree (cmd.exe + java children)
            all_procs = [parent] + parent.children(recursive=True)

            # Najdi hlavní Java proces (největší RAM v tree)
            main = max(all_procs, key=lambda p: p.memory_info().rss, default=parent)
            main_pid = main.pid

            # CPU: použij cached Process objekty (první volání vždy vrátí 0)
            cpu = 0.0
            for p in all_procs:
                if not p.is_running():
                    continue
                cached = _proc_cache.get(p.pid)
                if cached is None:
                    # První setkání — inicializuj měření, vrátí 0
                    _proc_cache[p.pid] = p
                    p.cpu_percent()
                else:
                    try:
                        cpu += cached.cpu_percent()
                    except psutil.NoSuchProcess:
                        _proc_cache.pop(p.pid, None)

            rss     = sum(p.memory_info().rss for p in all_procs if p.is_running())
            threads = main.num_threads()

            return ProcessInfo(
                pid=main_pid,
                status=ServerStatus.ONLINE,
                cpu_percent=cpu,
                rss_bytes=rss,
                thread_count=threads,
                tmux_alive=False,
            )
        except psutil.NoSuchProcess:
            _processes.pop(server.slug, None)
            stop_console_capture(server.slug)
            self._clear_pid(server)
            return ProcessInfo(pid=None, status=ServerStatus.OFFLINE,
                               cpu_percent=0.0, rss_bytes=0, thread_count=0, tmux_alive=False)

    def ensure_console_capture(self, server: Server) -> bool:
        return ensure_console_capture(server)
