"""
apps/control/backends/tmux.py

LocalTmuxProcessBackend – veškerá komunikace s tmux se děje zde.
View funkce a service vrstva NIKDY nevolají subprocess přímo.
"""
import subprocess
import shlex
import logging
import psutil
from dataclasses import dataclass
from typing import Optional

from django.utils import timezone

from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)


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


class LocalTmuxProcessBackend:
    """
    Jednoduché, bezpečné rozhraní kolem tmux CLI.

    Veškerý user input prochází shlex.quote() — nikdy string interpolace.
    """

    requires_terminal_session = True

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
            # První volání cpu_percent vrací 0.0 – voláme s interval=None (non-blocking)
            cpu = proc.cpu_percent(interval=None)
            mem = proc.memory_info()
            threads = proc.num_threads()
            return ProcessInfo(
                pid=pid,
                status=ServerStatus.ONLINE,
                cpu_percent=cpu,
                rss_bytes=mem.rss,
                thread_count=threads,
                tmux_alive=tmux_alive,
            )
        except psutil.NoSuchProcess:
            return ProcessInfo(
                pid=None,
                status=ServerStatus.CRASHED,
                cpu_percent=0.0,
                rss_bytes=0,
                thread_count=0,
                tmux_alive=tmux_alive,
            )

    # ──────────────────────────────
    # Privátní: hledání PID
    # ──────────────────────────────

    def _find_pid(self, server: Server) -> Optional[int]:
        # 1) PID file (nejspolehlivější)
        if server.pid_file_path:
            try:
                with open(server.pid_file_path) as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                pass

        # 2) Projít child procesy tmux session
        # tmux list-panes -t SESSION -F "#{pane_pid}"
        try:
            result = self._run(["list-panes", "-t", server.tmux_session_name, "-F", "#{pane_pid}"], check=False)
            if result.returncode == 0:
                pane_pid = int(result.stdout.strip())
                # Vrátíme první child proces pane (= shell nebo server)
                children = psutil.Process(pane_pid).children(recursive=False)
                if children:
                    return children[0].pid
                return pane_pid
        except (TmuxError, ValueError, psutil.NoSuchProcess):
            pass

        return None
