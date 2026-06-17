"""
apps/control/management/commands/run_runtime_worker.py

Unified runtime worker – nahrazuje tři oddělené procesy jedním:
  - Console tailer  (každých 0.25 s)
  - Metrics collector (každých 12 s)
  - Server watchdog   (každých 5 s)

Každá smyčka běží v samostatném daemon threadu.
Hlavní thread čeká na SIGTERM/SIGINT a pak nastaví stop_event.
Heartbeaty se ukládají do Django cache (klíč runtime.heartbeat.*),
odkud je čte stránka /runtime/.
"""

import os
import signal
import time
import logging
import threading
from datetime import timedelta
from pathlib import Path

import psutil
import requests
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.cache import cache
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from apps.servers.models import Server, ServerStatus
from apps.console.buffer import append_console_lines, get_console_lines, touch_console_activity
from apps.metrics.models import MetricSample
from apps.metrics.aggregator import aggregate_to_minutes, aggregate_to_hours, run_retention_cleanup
from apps.audit.models import AuditEvent
from apps.control.service import _get_backend as _get_process_backend, restart_server, start_server
from apps.control.startup_probe import is_startup_ready

logger = logging.getLogger(__name__)

# ── Konstanty ──────────────────────────────────────────────────────────────────
CONSOLE_INTERVAL    = 0.25
METRICS_INTERVAL    = 12
WATCHDOG_INTERVAL   = 5
SCHEDULER_INTERVAL  = 30   # sekund – jak často kontrolovat cron plány

HEARTBEAT_SCHEDULER = "runtime.heartbeat.scheduler"

CRASH_THRESHOLD   = 3
LOG_SILENCE_WARN  = 120
LOG_SILENCE_CRASH = 300

HEARTBEAT_TTL = 120   # sekund – cache TTL pro heartbeat záznamy

HEARTBEAT_CONSOLE  = "runtime.heartbeat.console"
HEARTBEAT_METRICS  = "runtime.heartbeat.metrics"
HEARTBEAT_WATCHDOG = "runtime.heartbeat.watchdog"
DESIRED_STATE_RESTORE_TTL = 30


# ══════════════════════════════════════════════════════════════════════════════
# Console tailer
# ══════════════════════════════════════════════════════════════════════════════

class _LogTailer:
    """Sleduje jeden log soubor – detekuje rotaci a truncaci."""

    def __init__(self, server):
        self.server = server
        self.path   = Path(server.log_file_path)
        self._fh    = None
        self._inode = None

    TAIL_BYTES = 32 * 1024  # při prvním otevření přečti posledních 32 KB

    def open(self):
        try:
            self._fh    = open(self.path, "r", encoding="utf-8", errors="replace")
            self._inode = os.fstat(self._fh.fileno()).st_ino
            size = os.fstat(self._fh.fileno()).st_size
            if size > self.TAIL_BYTES:
                self._fh.seek(size - self.TAIL_BYTES)
                self._fh.readline()  # přeskočí neúplný první řádek
            else:
                self._fh.seek(0)
            return True
        except OSError:
            self._fh = self._inode = None
            return False

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def read_new_lines(self):
        if self._fh is None:
            if not self.open():
                return []
        try:
            stat = self.path.stat()
            # inode check – na Windows je st_ino vždy 0, přeskočíme
            if self._inode and stat.st_ino and stat.st_ino != self._inode:
                logger.info("[%s] Log rotation", self.server.slug)
                self.close(); self.open(); return []
            if stat.st_size < self._fh.tell():
                logger.info("[%s] Log truncation", self.server.slug)
                self.close(); self.open(); return []
        except OSError:
            self.close()
            return []
        lines = []
        try:
            while True:
                line = self._fh.readline()
                if not line:
                    break
                lines.append(line.rstrip("\n"))
        except OSError as e:
            logger.warning("[%s] Chyba čtení: %s", self.server.slug, e)
            self.close()
        return lines


def _check_startup(server, lines):
    if server.status != ServerStatus.STARTING:
        return
    from apps.servers.adapters import get_adapter
    adapter = get_adapter(server.game_type)
    for line in lines:
        if adapter.is_startup_complete(line):
            logger.info("[%s] Startup confirmed: %s", server.slug, line[:80])
            server.status = ServerStatus.ONLINE
            server.save(update_fields=["status", "updated_at"])
            try:
                state = server.process_state
                state.status = ServerStatus.ONLINE
                state.consecutive_failures = 0
                state.save(update_fields=["status", "consecutive_failures"])
            except Exception:
                pass
            AuditEvent.objects.create(
                server=server, event_type="server.start.confirmed",
                severity="info", message=f"Startup: {line[:100]}",
            )
            break


def _console_loop(stop_event: threading.Event):
    logger.info("Console tailer thread spuštěn.")
    backend = _get_process_backend()
    if not getattr(backend, "requires_terminal_session", False):
        logger.info("Console tailer přeskočen: backend používá direct process console capture.")
        while not stop_event.is_set():
            cache.set(HEARTBEAT_CONSOLE, timezone.now().isoformat(), HEARTBEAT_TTL)
            stop_event.wait(CONSOLE_INTERVAL)
        logger.info("Console tailer thread ukončen.")
        return

    channel_layer = get_channel_layer()
    tailers: dict[str, _LogTailer] = {}

    while not stop_event.is_set():
        try:
            active = list(
                Server.objects.filter(is_active=True)
                              .exclude(status=ServerStatus.OFFLINE)
                              .select_related("process_state")
            )
            active_slugs = {s.slug for s in active}

            for server in active:
                if not server.log_file_path:
                    continue
                if server.slug not in tailers:
                    tailers[server.slug] = _LogTailer(server)
                else:
                    # Pokud se log_file_path změnil (auto-nastavení při startu), aktualizuj tailer
                    existing = tailers[server.slug]
                    if str(existing.path) != server.log_file_path:
                        existing.close()
                        tailers[server.slug] = _LogTailer(server)

            for slug in list(tailers):
                if slug not in active_slugs:
                    tailers[slug].close()
                    del tailers[slug]

            for server in active:
                tailer = tailers.get(server.slug)
                if not tailer:
                    continue
                new_lines = tailer.read_new_lines()
                if not new_lines:
                    continue

                append_console_lines(
                    server.id,
                    [("stdout", line) for line in new_lines],
                    source="log_tail",
                )

                try:
                    touch_console_activity(server, timezone.now())
                except Exception:
                    pass

                group = f"server.{server.id}.console"
                for line in new_lines:
                    try:
                        async_to_sync(channel_layer.group_send)(
                            group,
                            {
                                "type":      "console.line",
                                "server_id": str(server.id),
                                "timestamp": timezone.now().isoformat(),
                                "line":      line,
                            }
                        )
                    except Exception as e:
                        logger.warning("WS push (console) selhal: %s", e)

                for line in new_lines:
                    try:
                        from apps.servers.player_tracker import process_line_for_players
                        process_line_for_players(server, line)
                    except Exception as e:
                        logger.debug("player_tracker chyba: %s", e)

                    try:
                        from apps.alerts.engine import check_log_pattern_alert
                        check_log_pattern_alert(server, line)
                    except Exception as e:
                        logger.debug("alert engine chyba: %s", e)

                _check_startup(server, new_lines)

            cache.set(HEARTBEAT_CONSOLE, timezone.now().isoformat(), HEARTBEAT_TTL)

        except Exception:
            logger.exception("Console tailer: neočekávaná chyba v hlavní smyčce")

        stop_event.wait(CONSOLE_INTERVAL)

    for t in tailers.values():
        t.close()
    logger.info("Console tailer thread ukončen.")


# ══════════════════════════════════════════════════════════════════════════════
# Metrics collector
# ══════════════════════════════════════════════════════════════════════════════

def _metrics_loop(stop_event: threading.Event):
    logger.info("Metrics collector thread spuštěn.")
    channel_layer = get_channel_layer()
    backend = _get_process_backend()

    # Stav agregací – lokální pro tento thread (nahrazuje module-level globals)
    last_minute_agg: dict = {}
    last_hour_agg:   dict = {}
    last_cleanup = None

    # Inicializuj CPU countery (první volání dá vždy 0 %)
    try:
        psutil.cpu_percent(interval=None)
        for proc in psutil.process_iter(["cpu_percent"]):
            pass
    except Exception:
        pass

    while not stop_event.is_set():
        try:
            now = timezone.now()

            for server in Server.objects.filter(
                is_active=True,
                status__in=[ServerStatus.ONLINE, ServerStatus.STARTING],
            ).select_related("process_state"):
                try:
                    _metrics_collect(server, channel_layer, backend, now)
                    _metrics_maybe_aggregate(server, now, last_minute_agg, last_hour_agg)
                    _metrics_run_alerts(server)
                except Exception as exc:
                    logger.warning("Metrics chyba pro %s: %s", server.slug, exc)

            last_cleanup = _metrics_maybe_cleanup(now, last_cleanup)

            cache.set(HEARTBEAT_METRICS, timezone.now().isoformat(), HEARTBEAT_TTL)

        except Exception:
            logger.exception("Metrics collector: neočekávaná chyba v hlavní smyčce")

        stop_event.wait(METRICS_INTERVAL)

    logger.info("Metrics collector thread ukončen.")


def _metrics_collect(server, channel_layer, backend, now):
    info    = backend.get_process_info(server)

    disk_used = disk_free = None
    try:
        disk = psutil.disk_usage(server.working_directory)
        disk_used, disk_free = disk.used, disk.free
    except Exception:
        pass

    net_rx = net_tx = None
    try:
        net = psutil.net_io_counters()
        net_rx, net_tx = net.bytes_recv, net.bytes_sent
    except Exception:
        pass

    state = server.process_state
    MetricSample.objects.create(
        server             = server,
        timestamp          = now,
        cpu_percent        = info.cpu_percent,
        ram_bytes          = info.rss_bytes,
        thread_count       = info.thread_count,
        disk_used_bytes    = disk_used,
        disk_free_bytes    = disk_free,
        net_rx_bytes       = net_rx,
        net_tx_bytes       = net_tx,
        player_count       = state.last_player_count if state else None,
    )

    state.pid               = info.pid
    state.cpu_percent_last  = info.cpu_percent
    state.rss_bytes_last    = info.rss_bytes
    state.thread_count_last = info.thread_count
    state.last_healthcheck_at = now
    state.save(update_fields=[
        "pid", "cpu_percent_last", "rss_bytes_last",
        "thread_count_last", "last_healthcheck_at",
    ])

    async_to_sync(channel_layer.group_send)(
        f"server.{server.id}.metrics",
        {
            "type":            "metrics.snapshot",
            "server_id":       str(server.id),
            "timestamp":       now.isoformat(),
            "cpu_percent":     info.cpu_percent,
            "ram_bytes":       info.rss_bytes,
            "threads":         info.thread_count,
            "players":         state.last_player_count if state else None,
            "pid":             info.pid,
            "disk_free_bytes": disk_free,
        }
    )
    logger.debug("%s CPU=%.1f%% RAM=%dMB", server.slug,
                 info.cpu_percent or 0, (info.rss_bytes or 0) // 1_048_576)


def _metrics_maybe_aggregate(server, now, last_minute_agg, last_hour_agg):
    sid = str(server.id)
    last_min = last_minute_agg.get(sid)
    if last_min is None or (now - last_min).seconds >= 60:
        aggregate_to_minutes(server)
        last_minute_agg[sid] = now
    last_hr = last_hour_agg.get(sid)
    if last_hr is None or (now - last_hr).seconds >= 3600:
        aggregate_to_hours(server)
        last_hour_agg[sid] = now


def _metrics_run_alerts(server):
    try:
        from apps.alerts.engine import check_metric_alerts
        state = server.process_state
        check_metric_alerts(
            server,
            cpu_percent=state.cpu_percent_last or 0,
            ram_bytes=state.rss_bytes_last or 0,
        )
    except Exception as exc:
        logger.debug("Metric alert check chyba: %s", exc)


def _metrics_maybe_cleanup(now, last_cleanup):
    if last_cleanup is None or (now - last_cleanup) >= timedelta(hours=24):
        result = run_retention_cleanup()
        logger.info("Daily cleanup: %s", result)
        return now
    return last_cleanup


# ══════════════════════════════════════════════════════════════════════════════
# Server watchdog
# ══════════════════════════════════════════════════════════════════════════════

def _watchdog_loop(stop_event: threading.Event):
    logger.info("Watchdog thread spuštěn.")
    backend       = _get_process_backend()
    channel_layer = get_channel_layer()

    no_players_tick = 0  # každých ~60 s  (12 × 5 s)
    backup_tick     = 0  # každých ~720 s (144 × 5 s = 12 minut)

    while not stop_event.is_set():
        try:
            no_players_tick += 1
            backup_tick     += 1
            check_no_players = (no_players_tick >= 12)
            check_backup     = (backup_tick     >= 144)
            if check_no_players: no_players_tick = 0
            if check_backup:     backup_tick     = 0

            for server in Server.objects.filter(is_active=True).select_related("process_state"):
                try:
                    _watchdog_check(server, backend, channel_layer)
                    _watchdog_reconcile_desired_state(server)
                    if check_no_players and server.status == ServerStatus.ONLINE:
                        from apps.alerts.engine import check_no_players_alert
                        check_no_players_alert(server)
                    if check_backup and server.backup_directory:
                        _check_backup(server)
                except Exception as exc:
                    logger.exception("Watchdog chyba pro %s: %s", server.slug, exc)

            cache.set(HEARTBEAT_WATCHDOG, timezone.now().isoformat(), HEARTBEAT_TTL)

        except Exception:
            logger.exception("Watchdog: neočekávaná chyba v hlavní smyčce")

        stop_event.wait(WATCHDOG_INTERVAL)

    logger.info("Watchdog thread ukončen.")


def _check_backup(server):
    """Zkontroluje stáří backupů, pokud jsou staré – spustí novou zálohu."""
    from apps.servers.backup import check_backup_status
    from apps.servers.backup_engine import create_backup
    from apps.audit.models import AuditEvent

    result = check_backup_status(server)
    if result.get("ok") is False:
        logger.warning("[%s] Backup problém: %s – spouštím zálohu", server.slug, result.get("message"))
        backup_result = create_backup(server)
        if not backup_result.get("ok"):
            AuditEvent.objects.create(
                server=server,
                event_type="server.backup.stale",
                severity="warning",
                message=result.get("message", "Backup je starý nebo chybí."),
                payload_json={**result, "auto_backup": backup_result},
            )
    elif result.get("ok") is None:
        pass  # Backup adresář není nastaven – ignoruj
    else:
        logger.debug("[%s] Backup OK: %s", server.slug, result.get("message"))


def _watchdog_check(server, backend, channel_layer):
    state      = server.process_state
    now        = timezone.now()
    info       = backend.get_process_info(server)
    old_status = server.status

    state.pid               = info.pid
    state.cpu_percent_last  = info.cpu_percent
    state.rss_bytes_last    = info.rss_bytes
    state.thread_count_last = info.thread_count
    state.last_healthcheck_at = now

    new_status = _watchdog_derive(server, state, info, now, backend)

    state.status = new_status
    state.save()

    if new_status != old_status:
        server.status       = new_status
        server.last_seen_at = now
        server.save(update_fields=["status", "last_seen_at", "updated_at"])
        _watchdog_on_transition(server, old_status, new_status, channel_layer)


def _watchdog_derive(server, state, info, now, backend):
    cur = server.status
    session_required = getattr(backend, "requires_terminal_session", False)
    session_alive = info.tmux_alive if session_required else bool(info.pid)

    if cur in (ServerStatus.OFFLINE, ServerStatus.CRASHED) and info.pid and session_alive:
        state.consecutive_failures = 0
        if _watchdog_startup_confirmed(server):
            return ServerStatus.ONLINE
        if not state.started_at:
            state.started_at = now
        return ServerStatus.STARTING

    if cur == ServerStatus.STARTING:
        elapsed = (now - state.started_at).seconds if state.started_at else 0
        timeout = server.expected_startup_seconds
        if info.pid:
            if _watchdog_startup_confirmed(server):
                state.consecutive_failures = 0
                return ServerStatus.ONLINE
            if elapsed > timeout:
                logger.warning("%s: startup timeout %ds, proces běží ale ještě není připojitelný", server.slug, elapsed)
        else:
            if elapsed > timeout:
                state.last_error = f"Startup timeout ({elapsed}s)"
                state.consecutive_failures += 1
                return ServerStatus.CRASHED
        return ServerStatus.STARTING

    if cur == ServerStatus.STOPPING:
        elapsed = (now - state.last_command_at).seconds if state.last_command_at else 0
        timeout = server.expected_shutdown_seconds
        if not info.pid and not session_alive:
            state.stopped_at = now
            state.consecutive_failures = 0
            return ServerStatus.OFFLINE
        if elapsed > timeout * 2:
            state.last_error = f"Shutdown timeout ({elapsed}s)"
            logger.error("%s: shutdown timeout → CRASHED", server.slug)
            return ServerStatus.CRASHED
        return ServerStatus.STOPPING

    if cur == ServerStatus.ONLINE:
        if info.pid and session_alive:
            state.consecutive_failures = 0
            if state.last_log_line_at:
                silence = (now - state.last_log_line_at).seconds
                if silence > LOG_SILENCE_WARN:
                    logger.warning("%s: log silence %ds, proces ale stále běží", server.slug, silence)
            return ServerStatus.ONLINE
        else:
            state.consecutive_failures += 1
            logger.warning("%s: healthcheck selhal (%d/%d)",
                           server.slug, state.consecutive_failures, CRASH_THRESHOLD)
            if state.consecutive_failures >= CRASH_THRESHOLD:
                state.last_error = (
                    f"Proces zmizel po {state.consecutive_failures} selhání, "
                    f"session_alive={session_alive}"
                )
                return ServerStatus.CRASHED
            return ServerStatus.ONLINE

    if cur == ServerStatus.UNKNOWN:
        if info.pid and session_alive:
            state.consecutive_failures = 0
            return ServerStatus.ONLINE
        state.consecutive_failures += 1
        if state.consecutive_failures >= CRASH_THRESHOLD:
            state.last_error = "Přetrvávající UNKNOWN → CRASHED"
            return ServerStatus.CRASHED
        return ServerStatus.UNKNOWN

    return cur


def _watchdog_startup_confirmed(server):
    return is_startup_ready(server)


def _watchdog_push_console_state(server, channel_layer):
    try:
        state = _get_process_backend().get_console_state(server) or {}
        async_to_sync(channel_layer.group_send)(
            f"server.{server.id}.console",
            {
                "type": "console.state",
                "available": state.get("available", False),
                "can_write": state.get("can_write", False),
                "message": state.get("message", ""),
            }
        )
    except Exception as exc:
        logger.debug("console.state push selhal: %s", exc)


def _watchdog_on_transition(server, old_status, new_status, channel_layer):
    severity = "error" if new_status in (ServerStatus.CRASHED,) else "info"
    msg = f"Status: {old_status} → {new_status}"
    logger.info("[%s] %s", server.slug, msg)

    AuditEvent.objects.create(
        server=server, event_type="server.status.changed",
        severity=severity, message=msg,
    )

    ts = timezone.now().isoformat()
    async_to_sync(channel_layer.group_send)(
        f"server.{server.id}.status",
        {"type": "server.status", "server_id": str(server.id), "status": new_status}
    )
    _watchdog_push_console_state(server, channel_layer)
    async_to_sync(channel_layer.group_send)(
        f"server.{server.id}.events",
        {
            "type": "audit.event", "event_type": "server.status.changed",
            "severity": severity, "message": msg, "timestamp": ts,
        }
    )

    _watchdog_maybe_webhook(server, new_status)

    try:
        from apps.alerts.engine import check_status_alerts
        check_status_alerts(server, new_status)
    except Exception as exc:
        logger.warning("Alert engine chyba: %s", exc)

    if new_status == ServerStatus.OFFLINE:
        from apps.control.restart_handler import maybe_auto_restart
        maybe_auto_restart(server)


def _watchdog_maybe_webhook(server, new_status):
    if not server.webhook_url:
        return
    should = (
        (new_status == ServerStatus.CRASHED and server.webhook_on_crash) or
        (new_status == ServerStatus.ONLINE  and server.webhook_on_start)  or
        (new_status == ServerStatus.OFFLINE and server.webhook_on_stop)
    )
    if not should:
        return
    icons = {
        ServerStatus.CRASHED: "🔴",
        ServerStatus.ONLINE:  "🟢",
        ServerStatus.OFFLINE: "⚫",
    }
    try:
        requests.post(
            server.webhook_url,
            json={"content": f"{icons.get(new_status, '🟡')} **{server.name}**: `{new_status}`"},
            timeout=5,
        ).raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Webhook selhal pro %s: %s", server.slug, exc)


def _watchdog_reconcile_desired_state(server):
    if not server.desired_running:
        return
    if server.status not in (ServerStatus.OFFLINE, ServerStatus.CRASHED):
        return

    retry_key = f"desired_restore:{server.id}"
    if cache.get(retry_key):
        return
    cache.set(retry_key, True, DESIRED_STATE_RESTORE_TTL)

    logger.info("[%s] Obnovuji pozadovany stav: desired_running=True, status=%s", server.slug, server.status)
    result = start_server(server, user=None)
    if result.get("ok"):
        AuditEvent.objects.create(
            server=server,
            event_type="server.restore.dispatched",
            severity="warning",
            message=f"Automaticka obnova pozadovaneho stavu: {result.get('message', 'start odeslan')}",
            payload_json={"desired_running": True, "previous_status": server.status},
        )
        return

    logger.warning("[%s] Automaticka obnova selhala: %s", server.slug, result.get("message"))


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler (plánované restarty)
# ══════════════════════════════════════════════════════════════════════════════

def _scheduler_loop(stop_event: threading.Event):
    """Každých 30 s zkontroluje aktivní ScheduledRestart záznamy a spustí restart."""
    logger.info("Scheduler thread spuštěn.")

    try:
        from croniter import croniter as Croniter
    except ImportError:
        logger.error("croniter není nainstalován – scheduler neběží. Nainstaluj: pip install croniter")
        return

    from apps.servers.models import ScheduledRestart

    while not stop_event.is_set():
        try:
            now = timezone.localtime()
            # Budeme porovnávat s oknem ±SCHEDULER_INTERVAL sekund aby nedošlo k vynechání při drobném zpoždění
            window = timedelta(seconds=SCHEDULER_INTERVAL)

            active = ScheduledRestart.objects.filter(
                is_active=True,
                server__is_active=True,
            ).select_related("server")

            for sched in active:
                try:
                    cron = Croniter(sched.cron_expression, now - window)
                    next_dt = cron.get_next(type(now))
                    # Spustit pokud by příštím krok byl v minulém okně (tj. měl jsme ho spustit)
                    if next_dt <= now:
                        # Zkontroluj jestli jsme to v tomto okně ještě nespustili
                        if sched.last_triggered_at and (now - sched.last_triggered_at) < window:
                            continue  # Už spuštěno v tomto okně
                        _scheduler_trigger(sched)
                except Exception as exc:
                    logger.warning("Scheduler: chyba pro plán %d (%s): %s", sched.pk, sched.cron_expression, exc)

            cache.set(HEARTBEAT_SCHEDULER, timezone.now().isoformat(), HEARTBEAT_TTL)

        except Exception:
            logger.exception("Scheduler: neočekávaná chyba v hlavní smyčce")

        stop_event.wait(SCHEDULER_INTERVAL)

    logger.info("Scheduler thread ukončen.")


def _scheduler_trigger(sched):
    """Spustí restart serveru dle plánu s varováními."""
    import time as _time
    from apps.servers.models import ServerStatus, Server
    from apps.audit.models import AuditEvent

    # Načti čerstvý stav serveru
    server = Server.objects.get(pk=sched.server_id)
    logger.info("[scheduler] Spouštím plánovaný restart: %s (%s)", server.slug, sched.cron_expression)

    backend = _get_process_backend()

    def send_warn(msg):
        try:
            backend.send_command(server, f"say {msg}")
        except Exception as exc:
            logger.debug("[scheduler] warn send chyba: %s", exc)

    # Sada varování: [(minuty_pred, zprava), ...]
    warn_steps = []
    total = sched.warn_minutes
    if total >= 10:
        warn_steps.append((total,    f"[RESTART] Server se restartuje za {total} minut!"))
        warn_steps.append((5,        "[RESTART] Server se restartuje za 5 minut!"))
        warn_steps.append((1,        "[RESTART] Server se restartuje za 1 minutu!"))
    elif total >= 5:
        warn_steps.append((total,    f"[RESTART] Server se restartuje za {total} minut!"))
        warn_steps.append((1,        "[RESTART] Server se restartuje za 1 minutu!"))
    elif total > 0:
        warn_steps.append((total,    f"[RESTART] Server se restartuje za {total} minut!"))

    if warn_steps and server.status == ServerStatus.ONLINE:
        prev_minutes = warn_steps[0][0]
        for (minutes, msg) in warn_steps:
            sleep_sec = (prev_minutes - minutes) * 60
            if sleep_sec > 0:
                _time.sleep(sleep_sec)
            server.refresh_from_db(fields=["status"])
            if server.status != ServerStatus.ONLINE:
                break
            send_warn(msg)
            prev_minutes = minutes
        # Spánkek na poslední minutu
        _time.sleep(warn_steps[-1][0] * 60)

    # Restart
    server.refresh_from_db(fields=["status"])
    try:
        if server.status in (ServerStatus.ONLINE, ServerStatus.UNKNOWN, ServerStatus.STARTING):
            result = restart_server(server, user=None)
        else:
            result = start_server(server, user=None)

        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "Plánovaný restart selhal.")

        AuditEvent.objects.create(
            server=server,
            event_type="server.scheduled_restart",
            severity="info",
            message=f"Plánovaný restart '{sched.get_label()}' ({sched.cron_expression})",
        )
        logger.info("[scheduler] Restart %s dokončen.", server.slug)
    except Exception as exc:
        logger.error("[scheduler] Restart %s selhal: %s", server.slug, exc)
        AuditEvent.objects.create(
            server=server,
            event_type="server.scheduled_restart.failed",
            severity="error",
            message=str(exc),
        )

    sched.last_triggered_at = timezone.now()
    sched.save(update_fields=["last_triggered_at"])


# ══════════════════════════════════════════════════════════════════════════════
# Management command
# ══════════════════════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = "Unified runtime worker (console tailer + metrics collector + watchdog)."

    def handle(self, *args, **options):
        self.stdout.write("Runtime worker spouštím...")

        # Počkej dokud není setup wizard dokončen
        from apps.setup.models import BootstrapState
        if not BootstrapState.is_done():
            self.stdout.write("⏳ Setup wizard nebyl dokončen – workři čekají...")
            while not BootstrapState.is_done():
                time.sleep(5)
            self.stdout.write("✅ Setup wizard dokončen – spouštím workry.")

        stop_event, threads = start_runtime_threads()

        # Signály musí být registrovány v hlavním threadu
        def _shutdown(signum, frame):
            self.stdout.write(f"\nRuntime worker: signál {signum}, ukončuji...")
            stop_event.set()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT,  _shutdown)

        self.stdout.write(
            f"Runtime worker běží ({len(threads)} threadů: "
            "console-tailer, metrics-collector, server-watchdog, restart-scheduler). "
            "Stiskni Ctrl+C nebo pošli SIGTERM pro ukončení."
        )

        # Hlavní thread čeká na stop_event – pak počká na dokončení threadů
        stop_event.wait()

        self.stdout.write("Runtime worker: čekám na dokončení threadů...")
        for t in threads:
            t.join(timeout=10)
            if t.is_alive():
                logger.warning("Thread %s se neukončil do 10 s.", t.name)

        # Vymaž heartbeaty aby status stránka ukázala STOPPED
        cache.delete(HEARTBEAT_CONSOLE)
        cache.delete(HEARTBEAT_METRICS)
        cache.delete(HEARTBEAT_WATCHDOG)
        cache.delete(HEARTBEAT_SCHEDULER)

        self.stdout.write("Runtime worker ukončen.")


def start_runtime_threads(stop_event: threading.Event | None = None):
    stop_event = stop_event or threading.Event()
    threads = [
        threading.Thread(target=_console_loop,    args=(stop_event,), name="console-tailer",      daemon=True),
        threading.Thread(target=_metrics_loop,    args=(stop_event,), name="metrics-collector",   daemon=True),
        threading.Thread(target=_watchdog_loop,   args=(stop_event,), name="server-watchdog",     daemon=True),
        threading.Thread(target=_scheduler_loop,  args=(stop_event,), name="restart-scheduler",   daemon=True),
    ]

    for t in threads:
        t.start()

    return stop_event, threads
