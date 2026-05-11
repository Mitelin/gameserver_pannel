"""
apps/control/backends/subprocess_backend.py

SubprocessBackend – spouští servery přímo přes subprocess.Popen.

stdout procesu jde přímo do log souboru (file redirect, ne PIPE).
Konzole se čte přes WebSocket file-tailer thread – přežije Django reload.
"""
import os
import sys
import time
import shlex
import logging
import subprocess
import threading
import queue
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import psutil

from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)

# slug → Popen
_processes: dict[str, subprocess.Popen] = {}

# slug → stop_event (pro file tailer)
_tailer_stop: dict[str, threading.Event] = {}

# Cache psutil.Process objektů — nutné pro správné CPU měření
_proc_cache: dict[int, psutil.Process] = {}


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


# ── File tailer: čte log soubor a posílá do WS + DB ──────────────────────────

def _file_tailer(slug: str, server_id: str, log_path: str, stop_event: threading.Event):
    """
    Sleduje log soubor (jako tail -f) a posílá nové řádky do WebSocket skupiny.
    Přežije Django reload – stačí ho znovu spustit při WS připojení.
    """
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    from django.utils import timezone

    channel_layer = get_channel_layer()
    group = f"server.{server_id}.console"

    logger.info("[%s] file_tailer spuštěn → %s", slug, log_path)

    fh = None
    batch = []

    def flush_batch():
        if not batch:
            return
        try:
            from apps.console.models import ConsoleLine
            from apps.servers.models import Server as Srv
            from django.db import connection
            connection.close()
            srv = Srv.objects.only("id").get(slug=slug)
            ConsoleLine.objects.bulk_create([
                ConsoleLine(server=srv, line=l, stream_type="stdout", source="subprocess")
                for l in batch
            ])
        except Exception as e:
            logger.debug("[%s] ConsoleLine flush: %s", slug, e)
        batch.clear()

    def open_file():
        nonlocal fh
        try:
            fh = open(log_path, "r", encoding="utf-8", errors="replace")
            fh.seek(0, 2)  # seek na konec – ukazuj jen nové řádky
            return True
        except Exception as e:
            logger.debug("[%s] tailer open: %s", slug, e)
            return False

    while not stop_event.is_set():
        # Počkej na vznik souboru
        if fh is None:
            if not open_file():
                stop_event.wait(0.5)
                continue

        line = fh.readline()
        if line:
            line = line.rstrip("\r\n")
            batch.append(line)
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
                logger.debug("[%s] WS push: %s", slug, e)

            if len(batch) >= 20:
                flush_batch()
        else:
            flush_batch()
            stop_event.wait(0.1)  # krátké čekání na nová data

    flush_batch()
    if fh:
        try:
            fh.close()
        except Exception:
            pass
    _tailer_stop.pop(slug, None)
    logger.info("[%s] file_tailer ukončen", slug)


def ensure_tailer(server: Server) -> bool:
    """
    Spustí file-tailer thread pokud ještě neběží.
    Volá se při startu serveru i při každém WS připojení klienta.
    Vrací True pokud tailer byl/je spuštěn.
    """
    slug = server.slug
    log_path = server.log_file_path.strip() if server.log_file_path else ""
    if not log_path:
        log_path = str(Path(server.working_directory) / "panel_output.log")

    # Zkontroluj jestli tailer ještě běží
    stop_ev = _tailer_stop.get(slug)
    if stop_ev and not stop_ev.is_set():
        return True  # tailer běží

    # Vytvoř nový stop event a spusť vlákno
    ev = threading.Event()
    _tailer_stop[slug] = ev
    threading.Thread(
        target=_file_tailer,
        args=(slug, str(server.id), log_path, ev),
        daemon=True, name=f"tailer-{slug}",
    ).start()
    logger.info("[%s] ensure_tailer: nový tailer spuštěn", slug)
    return True


def stop_tailer(slug: str):
    """Zastaví file-tailer thread."""
    ev = _tailer_stop.get(slug)
    if ev:
        ev.set()


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

        # Příprava log souboru
        log_path = server.log_file_path.strip() if server.log_file_path else ""
        if not log_path or Path(log_path).is_dir():
            log_path = str(Path(workdir) / "panel_output.log")
            server.log_file_path = log_path
            server.save(update_fields=["log_file_path"])

        # Otevři log soubor pro zápis – stdout serveru jde přímo sem
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except Exception as exc:
            raise SubprocessError(f"Nelze otevřít log soubor: {exc}") from exc

        logger.info("[%s] Spouštím '%s' v %s → %s", server.slug, start_cmd, workdir, log_path)
        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    start_cmd,
                    cwd=workdir,
                    shell=True,
                    stdin=subprocess.PIPE,
                    stdout=log_fh,
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
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
        except Exception as exc:
            log_fh.close()
            raise SubprocessError(f"Nelze spustit server: {exc}") from exc

        _processes[server.slug] = proc
        self._save_pid(server, proc.pid)

        # Spusť file tailer – čte log soubor a posílá do WS
        # Dáme mu chvíli aby se soubor začal plnit
        time.sleep(0.3)
        ensure_tailer(server)

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
        stop_tailer(server.slug)
        proc = _processes.pop(server.slug, None)
        if proc and proc.poll() is None:
            try:
                pid = proc.pid
                proc.kill()
                self._kill_tree(pid, "kill")
                return
            except Exception:
                pass
        pid = self._load_pid(server)
        if pid:
            self._kill_tree(pid, "kill")
        try:
            self._pid_file(server).unlink(missing_ok=True)
        except Exception:
            pass

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

    def get_process_info(self, server: Server) -> ProcessInfo:
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
            return ProcessInfo(pid=None, status=ServerStatus.CRASHED,
                               cpu_percent=0.0, rss_bytes=0, thread_count=0, tmux_alive=False)
