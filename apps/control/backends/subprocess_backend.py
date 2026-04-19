"""
apps/control/backends/subprocess_backend.py

SubprocessBackend – spouští servery přímo přes subprocess.Popen.
Funguje na Windows i Linuxu bez tmux.
PID se ukládá do <working_directory>/.panel_pid
Stdin handle se drží v paměti procesu (ztratí se po restartu panelu).
"""
import os
import sys
import signal
import shlex
import logging
import subprocess
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import psutil

from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)

# Drží stdin handles živých procesů: slug → Popen
_processes: dict[str, subprocess.Popen] = {}


def _resolve_start_command(server: Server) -> str:
    """
    Priorita:
    1. server.start_command pokud je vyplněný (admin override)
    2. aktivní profil (pokud start_command je prázdný)
    """
    if server.start_command.strip():
        return server.start_command.strip()
    try:
        profile = server.start_profiles.filter(is_active=True).first()
        if profile:
            return profile.build_command()
    except Exception:
        pass
    return ""


@dataclass
class ProcessInfo:
    pid: Optional[int]
    status: str
    cpu_percent: float
    rss_bytes: int
    thread_count: int
    tmux_alive: bool  # vždy False – kompatibilita s tmux backendem


class SubprocessError(Exception):
    pass


class SubprocessBackend:

    def _pid_file(self, server: Server) -> Path:
        return Path(server.working_directory) / ".panel_pid"

    def _save_pid(self, server: Server, pid: int):
        try:
            self._pid_file(server).write_text(str(pid))
        except Exception as exc:
            logger.warning("Nelze uložit PID soubor pro %s: %s", server.slug, exc)

    def _load_pid(self, server: Server) -> Optional[int]:
        # Nejdřív živý handle v paměti
        proc = _processes.get(server.slug)
        if proc is not None:
            if proc.poll() is None:
                return proc.pid
            else:
                _processes.pop(server.slug, None)

        # Fallback na PID soubor
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

    # ── Veřejné API (stejné jako TmuxBackend) ────────────────────────────────

    def start_server(self, server: Server) -> None:
        if self.session_exists(server):
            raise SubprocessError(f"Server '{server.name}' již běží (PID {self._load_pid(server)}).")

        workdir = server.working_directory
        if not os.path.isdir(workdir):
            raise SubprocessError(f"Pracovní adresář neexistuje: {workdir}")

        start_cmd = _resolve_start_command(server)

        # Urči cestu k log souboru – pokud není nastavena, použij výchozí
        log_path = server.log_file_path.strip() if server.log_file_path else ""
        if not log_path:
            log_path = str(Path(workdir) / "panel_output.log")
            # Ulož cestu zpět na server aby ji konzole tailer našel
            server.log_file_path = log_path
            server.save(update_fields=["log_file_path"])
            logger.info("log_file_path nebyl nastaven, použiji: %s", log_path)

        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            stdout = open(log_path, "a", encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Nelze otevřít log soubor %s: %s", log_path, exc)
            stdout = subprocess.DEVNULL

        logger.info("Spouštím '%s' v %s", start_cmd, workdir)
        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    start_cmd,
                    cwd=workdir,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                proc = subprocess.Popen(
                    shlex.split(start_cmd),
                    cwd=workdir,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            raise SubprocessError(f"Nelze spustit server: {exc}") from exc

        _processes[server.slug] = proc
        self._save_pid(server, proc.pid)
        logger.info("Spuštěn server %s, PID %d", server.slug, proc.pid)

    def stop_server(self, server: Server) -> None:
        stop_cmd = (server.stop_command or "stop").strip()
        proc = _processes.get(server.slug)

        if proc and proc.poll() is None:
            try:
                proc.stdin.write((stop_cmd + "\n").encode())
                proc.stdin.flush()
                logger.info("Stop command '%s' odeslán do stdin serveru %s", stop_cmd, server.slug)
                return
            except Exception as exc:
                logger.warning("Nelze zapsat do stdin %s: %s – zkouším SIGTERM", server.slug, exc)

        # Fallback – SIGTERM přes PID
        pid = self._load_pid(server)
        if pid:
            try:
                p = psutil.Process(pid)
                p.terminate()
                logger.info("SIGTERM odeslán procesu %d (%s)", pid, server.slug)
            except psutil.NoSuchProcess:
                pass

    def kill_server(self, server: Server) -> None:
        proc = _processes.pop(server.slug, None)
        if proc and proc.poll() is None:
            try:
                proc.kill()
                logger.warning("SIGKILL odeslán procesu %d (%s)", proc.pid, server.slug)
                return
            except Exception:
                pass

        pid = self._load_pid(server)
        if pid:
            try:
                psutil.Process(pid).kill()
                logger.warning("SIGKILL odeslán přes PID %d (%s)", pid, server.slug)
            except psutil.NoSuchProcess:
                pass

        try:
            self._pid_file(server).unlink(missing_ok=True)
        except Exception:
            pass

    def send_command(self, server: Server, command: str) -> None:
        proc = _processes.get(server.slug)
        if proc and proc.poll() is None:
            try:
                proc.stdin.write((command + "\n").encode())
                proc.stdin.flush()
                return
            except Exception as exc:
                raise SubprocessError(f"Nelze odeslat příkaz: {exc}") from exc
        raise SubprocessError("Server není spuštěný nebo nemá stdin handle (restart panelu?).")

    def get_process_info(self, server: Server) -> ProcessInfo:
        pid = self._load_pid(server)
        if pid is None:
            return ProcessInfo(pid=None, status=ServerStatus.OFFLINE,
                               cpu_percent=0.0, rss_bytes=0, thread_count=0, tmux_alive=False)
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                raise psutil.NoSuchProcess(pid)
            return ProcessInfo(
                pid=pid,
                status=ServerStatus.ONLINE,
                cpu_percent=proc.cpu_percent(interval=None),
                rss_bytes=proc.memory_info().rss,
                thread_count=proc.num_threads(),
                tmux_alive=False,
            )
        except psutil.NoSuchProcess:
            _processes.pop(server.slug, None)
            return ProcessInfo(pid=None, status=ServerStatus.CRASHED,
                               cpu_percent=0.0, rss_bytes=0, thread_count=0, tmux_alive=False)
