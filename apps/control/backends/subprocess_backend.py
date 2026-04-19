"""
apps/control/backends/subprocess_backend.py

SubprocessBackend – spouští servery přímo přes subprocess.Popen.
Funguje na Windows i Linuxu bez tmux.

stdout procesu se čte přes PIPE v dedikovaném vlákně a posílá
do WebSocket channel layer (konzole v reálném čase).
Stdin handle se drží v paměti procesu (ztratí se po restartu panelu).
"""
import os
import sys
import shlex
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import psutil

from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)

# slug → Popen
_processes: dict[str, subprocess.Popen] = {}
# slug → reader Thread
_readers: dict[str, threading.Thread] = {}


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


def _stdout_reader(slug: str, server_id: str, proc: subprocess.Popen, log_path: str):
    """
    Běží v samostatném vlákně pro každý spuštěný server.
    Čte stdout procesu řádek po řádku a:
      1. zapisuje do log souboru
      2. posílá do WebSocket channel layer (konzole v reálném čase)
      3. ukládá do ConsoleLine DB (history při reconnect)
    """
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    from django.utils import timezone

    channel_layer = get_channel_layer()
    group = f"server.{server_id}.console"

    log_fh = None
    if log_path:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("[%s] Nelze otevřít log soubor %s: %s", slug, log_path, e)

    logger.info("[%s] stdout reader spuštěn", slug)
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n") if isinstance(raw, str) else raw.decode("utf-8", errors="replace").rstrip("\r\n")

            # 1) Log soubor
            if log_fh:
                try:
                    log_fh.write(line + "\n")
                    log_fh.flush()
                except Exception:
                    pass

            # 2) WebSocket push
            try:
                async_to_sync(channel_layer.group_send)(
                    group,
                    {
                        "type":      "console.line",
                        "server_id": server_id,
                        "timestamp": timezone.now().isoformat(),
                        "line":      line,
                    },
                )
            except Exception as e:
                logger.debug("[%s] WS push selhal: %s", slug, e)

            # 3) ConsoleLine DB (pro historii při reconnect)
            try:
                from apps.console.models import ConsoleLine
                from apps.servers.models import Server as ServerModel
                srv = ServerModel.objects.only("id").get(slug=slug)
                ConsoleLine.objects.create(
                    server=srv,
                    line=line,
                    stream_type="stdout",
                    source="subprocess",
                )
            except Exception:
                pass

    except Exception as e:
        logger.warning("[%s] stdout reader chyba: %s", slug, e)
    finally:
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass
        _readers.pop(slug, None)
        logger.info("[%s] stdout reader ukončen", slug)


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

    def _pid_file(self, server: Server) -> Path:
        return Path(server.working_directory) / ".panel_pid"

    def _save_pid(self, server: Server, pid: int):
        try:
            self._pid_file(server).write_text(str(pid))
        except Exception as exc:
            logger.warning("Nelze uložit PID soubor pro %s: %s", server.slug, exc)

    def _load_pid(self, server: Server) -> Optional[int]:
        proc = _processes.get(server.slug)
        if proc is not None:
            if proc.poll() is None:
                return proc.pid
            else:
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

        # Log soubor – pokud je prázdný nebo je to adresář, použij výchozí
        log_path = server.log_file_path.strip() if server.log_file_path else ""
        if not log_path or Path(log_path).is_dir():
            log_path = str(Path(workdir) / "panel_output.log")
            server.log_file_path = log_path
            server.save(update_fields=["log_file_path"])

        logger.info("[%s] Spouštím '%s' v %s", server.slug, start_cmd, workdir)
        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    start_cmd,
                    cwd=workdir,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
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
                    stderr=subprocess.STDOUT,
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

        # Spusť reader vlákno – čte stdout a posílá do WS + DB
        t = threading.Thread(
            target=_stdout_reader,
            args=(server.slug, str(server.id), proc, log_path),
            daemon=True,
            name=f"reader-{server.slug}",
        )
        t.start()
        _readers[server.slug] = t

        logger.info("[%s] Spuštěn, PID %d, reader thread: %s", server.slug, proc.pid, t.name)

    def stop_server(self, server: Server) -> None:
        stop_cmd = (server.stop_command or "stop").strip()
        proc = _processes.get(server.slug)
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(stop_cmd + "\n")
                proc.stdin.flush()
                logger.info("[%s] Stop command '%s' odeslán", server.slug, stop_cmd)
                return
            except Exception as exc:
                logger.warning("[%s] Nelze zapsat do stdin: %s", server.slug, exc)
        pid = self._load_pid(server)
        if pid:
            try:
                psutil.Process(pid).terminate()
                logger.info("[%s] SIGTERM odeslán PID %d", server.slug, pid)
            except psutil.NoSuchProcess:
                pass

    def kill_server(self, server: Server) -> None:
        proc = _processes.pop(server.slug, None)
        if proc and proc.poll() is None:
            try:
                proc.kill()
                logger.warning("[%s] SIGKILL odeslán PID %d", server.slug, proc.pid)
                return
            except Exception:
                pass
        pid = self._load_pid(server)
        if pid:
            try:
                psutil.Process(pid).kill()
                logger.warning("[%s] SIGKILL přes PID %d", server.slug, pid)
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
                proc.stdin.write(command + "\n")
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
