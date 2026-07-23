"""
apps/control/backends/tmux.py

LocalTmuxProcessBackend – veškerá komunikace s tmux se děje zde.
View funkce a service vrstva NIKDY nevolají subprocess přímo.
"""
import subprocess
import shlex
import logging
import os
import psutil
from dataclasses import dataclass
from typing import Optional

from django.utils import timezone

from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)

SHELL_PROCESS_NAMES = {
    "ash",
    "bash",
    "busybox",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "ksh",
    "mksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "yash",
    "zsh",
}

HELPER_PROCESS_NAMES = {
    "cat",
    "env",
    "flock",
    "nice",
    "nohup",
    "sleep",
    "stdbuf",
    "tail",
    "tee",
    "timeout",
}


@dataclass
class ProcessInfo:
    pid: Optional[int]
    status: str          # hodnota z ServerStatus
    cpu_percent: float
    rss_bytes: int
    thread_count: int
    tmux_alive: bool


class TmuxError(Exception):
    pass


@dataclass
class _CachedCpuSample:
    process: psutil.Process
    create_time: Optional[float]
    initialized: bool = False


class LocalTmuxProcessBackend:
    """
    Jednoduché, bezpečné rozhraní kolem tmux CLI.

    Veškerý user input prochází shlex.quote() — nikdy string interpolace.
    """

    requires_terminal_session = True

    def __init__(self) -> None:
        self._cpu_samples: dict[int, _CachedCpuSample] = {}
        self._server_pids: dict[str, int] = {}

    # ──────────────────────────────
    # Privátní tmux helpery
    # ──────────────────────────────

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Spustí tmux příkaz, logguje chyby."""
        try:
            return subprocess.run(
                ["tmux"] + args,
                capture_output=True,
                text=True,
                timeout=10,
                check=check,
            )
        except subprocess.TimeoutExpired as e:
            raise TmuxError(f"tmux timeout: {e}") from e
        except subprocess.CalledProcessError as e:
            raise TmuxError(f"tmux error: {e.stderr.strip()}") from e

    def session_exists(self, server: Server) -> bool:
        result = self._run(["has-session", "-t", server.tmux_session_name], check=False)
        return result.returncode == 0

    # ──────────────────────────────
    # Veřejné API
    # ──────────────────────────────

    def start_server(self, server: Server) -> None:
        if self.session_exists(server):
            raise TmuxError(f"Session '{server.tmux_session_name}' již existuje.")

        # Priorita: manuální start_command (pokud vyplněný) > aktivní profil
        if server.start_command.strip():
            start_cmd = server.start_command.strip()
        else:
            start_cmd = server.start_command
            try:
                profile = server.start_profiles.filter(is_active=True).first()
                if profile:
                    start_cmd = profile.build_command()
            except Exception:
                pass

        cmd = (
            f"cd {shlex.quote(server.working_directory)} && "
            f"{start_cmd}"
        )
        self._run([
            "new-session", "-d",
            "-s", server.tmux_session_name,
            "-x", "220",
            "-y", "50",
            "bash", "-c", cmd,
        ])
        logger.info("Spuštěn server %s v tmux session %s", server.slug, server.tmux_session_name)

    def stop_server(self, server: Server) -> None:
        """Soft stop – pošle stop command do konzole."""
        stop_cmd = server.stop_command or "stop"
        self.send_command(server, stop_cmd)

    def kill_server(self, server: Server) -> None:
        """Force-stop – zabije celou tmux session."""
        if not self.session_exists(server):
            return
        self._run(["kill-session", "-t", server.tmux_session_name])
        logger.warning("Force-killed tmux session %s", server.tmux_session_name)

    def send_command(self, server: Server, command: str) -> None:
        """Pošle příkaz do tmux konzole serveru."""
        if not self.session_exists(server):
            raise TmuxError(f"Session '{server.tmux_session_name}' neexistuje.")
        # Enter na konci = C-m
        self._run([
            "send-keys", "-t", server.tmux_session_name,
            command, "C-m",
        ])

    def get_console_state(self, server: Server) -> dict:
        if not self.session_exists(server):
            return {
                "available": False,
                "can_write": False,
                "message": "Tmux session neběží.",
            }
        return {
            "available": True,
            "can_write": True,
            "message": "Konzole připojena přes tmux.",
        }

    def get_recent_console_lines(self, server: Server, limit: int = 200) -> list[str]:
        if not self.session_exists(server):
            return []
        limit = max(1, min(int(limit), 500))
        result = self._run(
            ["capture-pane", "-p", "-t", server.tmux_session_name, "-S", f"-{limit}"],
            check=False,
        )
        if result.returncode != 0:
            return []
        lines = [line.rstrip("\r") for line in result.stdout.splitlines()]
        return [line for line in lines if line.strip()]

    def get_process_info(self, server: Server) -> ProcessInfo:
        """
        Kombinuje tmux session check + psutil process lookup.
        Preferuje PID file, fallback na hledání přes working_directory.
        """
        tmux_alive = self.session_exists(server)
        pid = self._find_pid(server)

        if pid is None:
            self._clear_server_cpu_state(server.slug)
            return ProcessInfo(
                pid=None,
                status=ServerStatus.OFFLINE if not tmux_alive else ServerStatus.UNKNOWN,
                cpu_percent=0.0,
                rss_bytes=0,
                thread_count=0,
                tmux_alive=tmux_alive,
            )

        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                raise psutil.NoSuchProcess(pid)
            cpu = self._sample_cpu(server.slug, proc)
            try:
                mem = proc.memory_info()
                rss_bytes = mem.rss
            except psutil.AccessDenied:
                rss_bytes = 0
            try:
                threads = proc.num_threads()
            except psutil.AccessDenied:
                threads = 0
            return ProcessInfo(
                pid=pid,
                status=ServerStatus.ONLINE,
                cpu_percent=cpu,
                rss_bytes=rss_bytes,
                thread_count=threads,
                tmux_alive=tmux_alive,
            )
        except psutil.NoSuchProcess:
            self._clear_server_cpu_state(server.slug)
            return ProcessInfo(
                pid=None,
                status=ServerStatus.CRASHED,
                cpu_percent=0.0,
                rss_bytes=0,
                thread_count=0,
                tmux_alive=tmux_alive,
            )
        except psutil.AccessDenied:
            self._clear_server_cpu_state(server.slug, keep_pid=pid)
            return ProcessInfo(
                pid=pid,
                status=ServerStatus.ONLINE if tmux_alive else ServerStatus.UNKNOWN,
                cpu_percent=0.0,
                rss_bytes=0,
                thread_count=0,
                tmux_alive=tmux_alive,
            )

    # ──────────────────────────────
    # Privátní: hledání PID
    # ──────────────────────────────

    def _find_pid(self, server: Server) -> Optional[int]:
        pid_from_file = self._load_pid_file(server)
        if pid_from_file is not None:
            return pid_from_file

        for pane_pid in self._list_pane_pids(server):
            resolved = self._resolve_pane_server_pid(pane_pid)
            if resolved is not None:
                return resolved

        return None

    def _load_pid_file(self, server: Server) -> Optional[int]:
        if not server.pid_file_path:
            return None
        try:
            with open(server.pid_file_path) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return None
        return pid if psutil.pid_exists(pid) else None

    def _list_pane_pids(self, server: Server) -> list[int]:
        try:
            result = self._run(["list-panes", "-t", server.tmux_session_name, "-F", "#{pane_pid}"], check=False)
        except TmuxError:
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []

        pane_pids: list[int] = []
        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                pane_pids.append(int(stripped))
            except ValueError:
                continue
        return pane_pids

    def _resolve_pane_server_pid(self, pane_pid: int) -> Optional[int]:
        try:
            pane_proc = psutil.Process(pane_pid)
            if not pane_proc.is_running():
                return None
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        if self._is_server_process_candidate(pane_proc):
            return pane_proc.pid

        candidates = []
        for child in self._safe_children(pane_proc):
            if not self._is_server_process_candidate(child):
                continue
            candidates.append(child)

        if not candidates:
            return None

        best = max(candidates, key=self._candidate_sort_key)
        return best.pid

    def _sample_cpu(self, server_slug: str, proc: psutil.Process) -> float:
        pid = proc.pid
        cached_pid = self._server_pids.get(server_slug)
        if cached_pid != pid:
            self._clear_server_cpu_state(server_slug)
            self._server_pids[server_slug] = pid

        create_time = self._safe_create_time(proc)
        cached = self._cpu_samples.get(pid)
        if cached is None or cached.create_time != create_time:
            cached = _CachedCpuSample(process=proc, create_time=create_time)
            self._cpu_samples[pid] = cached

        if not cached.initialized:
            cached.process.cpu_percent(interval=None)
            cached.initialized = True
            return 0.0

        return cached.process.cpu_percent(interval=None)

    def _clear_server_cpu_state(self, server_slug: str, keep_pid: Optional[int] = None) -> None:
        cached_pid = self._server_pids.get(server_slug)
        if cached_pid is not None and cached_pid != keep_pid:
            self._cpu_samples.pop(cached_pid, None)
            self._server_pids.pop(server_slug, None)
        elif keep_pid is None:
            self._server_pids.pop(server_slug, None)

    def _safe_children(self, proc: psutil.Process) -> list[psutil.Process]:
        try:
            return proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

    def _is_server_process_candidate(self, proc: psutil.Process) -> bool:
        name = self._process_name(proc)
        if not name:
            return False
        if name in SHELL_PROCESS_NAMES:
            return False
        if name in HELPER_PROCESS_NAMES:
            return False
        return True

    def _candidate_sort_key(self, proc: psutil.Process) -> tuple[int, int, float, int]:
        return (
            self._safe_rss(proc),
            self._safe_thread_count(proc),
            -self._safe_create_time(proc, fallback=float("inf")),
            -proc.pid,
        )

    def _process_name(self, proc: psutil.Process) -> str:
        try:
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return ""
        return self._normalize_process_name(name)

    def _normalize_process_name(self, value: str) -> str:
        normalized = (value or "").strip().strip('"')
        if not normalized:
            return ""
        return os.path.basename(normalized).lower()

    def _safe_rss(self, proc: psutil.Process) -> int:
        try:
            return proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    def _safe_thread_count(self, proc: psutil.Process) -> int:
        try:
            return proc.num_threads()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    def _safe_create_time(self, proc: psutil.Process, fallback: Optional[float] = None) -> Optional[float]:
        try:
            return proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return fallback
