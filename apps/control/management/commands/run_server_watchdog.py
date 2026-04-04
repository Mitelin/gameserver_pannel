"""
apps/control/management/commands/run_server_watchdog.py  (fáze 3)

Přepracovaný watchdog:
  - Explicitní stavový automat s timeouty
  - Graceful shutdown na SIGTERM/SIGINT
  - Lepší crash detection s thresholdem
  - Audit eventy přes WS channel layer
  - Startup pattern detection přes GameAdapter
  - Webhook notifikace
"""
import signal
import time
import logging
import requests

from django.core.management.base import BaseCommand
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from apps.servers.models import Server, ServerStatus
from apps.audit.models import AuditEvent
from apps.control.backends.tmux import LocalTmuxProcessBackend

logger = logging.getLogger(__name__)

CHECK_INTERVAL    = 5
CRASH_THRESHOLD   = 3
LOG_SILENCE_WARN  = 120
LOG_SILENCE_CRASH = 300


class Command(BaseCommand):
    help = "Watchdog: sleduje stav, detekuje crash, notifikuje."

    def handle(self, *args, **options):
        self.stdout.write("Watchdog (fáze 3) spuštěn.")
        self._running = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

        backend       = LocalTmuxProcessBackend()
        channel_layer = get_channel_layer()

        while self._running:
            for server in Server.objects.filter(is_active=True).select_related("process_state"):
                try:
                    self._check(server, backend, channel_layer)
                except Exception as exc:
                    logger.exception("Watchdog chyba pro %s: %s", server.slug, exc)
            time.sleep(CHECK_INTERVAL)

        self.stdout.write("Watchdog ukončen.")

    def _shutdown(self, signum, frame):
        self.stdout.write(f"Watchdog: signál {signum}, ukončuji...")
        self._running = False

    def _check(self, server, backend, channel_layer):
        state      = server.process_state
        now        = timezone.now()
        info       = backend.get_process_info(server)
        old_status = server.status

        state.pid               = info.pid
        state.cpu_percent_last  = info.cpu_percent
        state.rss_bytes_last    = info.rss_bytes
        state.thread_count_last = info.thread_count
        state.last_healthcheck_at = now

        new_status = self._derive(server, state, info, now)

        state.status = new_status
        state.save()

        if new_status != old_status:
            server.status       = new_status
            server.last_seen_at = now
            server.save(update_fields=["status", "last_seen_at", "updated_at"])
            self._on_transition(server, old_status, new_status, channel_layer)

    def _derive(self, server, state, info, now):
        cur = server.status

        if cur == ServerStatus.STARTING:
            elapsed = (now - state.started_at).seconds if state.started_at else 0
            timeout = server.expected_startup_seconds
            if info.pid:
                if self._startup_confirmed(server):
                    state.consecutive_failures = 0
                    return ServerStatus.ONLINE
                if elapsed > timeout:
                    logger.warning("%s: startup timeout %ds, PID existuje → ONLINE", server.slug, elapsed)
                    state.consecutive_failures = 0
                    return ServerStatus.ONLINE
            else:
                if elapsed > timeout:
                    state.last_error = f"Startup timeout ({elapsed}s)"
                    state.consecutive_failures += 1
                    return ServerStatus.CRASHED
            return ServerStatus.STARTING

        if cur == ServerStatus.STOPPING:
            elapsed = (now - state.last_command_at).seconds if state.last_command_at else 0
            timeout = server.expected_shutdown_seconds
            if not info.pid and not info.tmux_alive:
                state.stopped_at = now
                state.consecutive_failures = 0
                return ServerStatus.OFFLINE
            if elapsed > timeout * 2:
                state.last_error = f"Shutdown timeout ({elapsed}s)"
                logger.error("%s: shutdown timeout → CRASHED", server.slug)
                return ServerStatus.CRASHED
            return ServerStatus.STOPPING

        if cur == ServerStatus.ONLINE:
            if info.pid and info.tmux_alive:
                state.consecutive_failures = 0
                if state.last_log_line_at:
                    silence = (now - state.last_log_line_at).seconds
                    if silence > LOG_SILENCE_CRASH and not info.pid:
                        state.last_error = f"Log silence {silence}s + no PID"
                        return ServerStatus.CRASHED
                    if silence > LOG_SILENCE_WARN:
                        logger.warning("%s: log silence %ds → UNKNOWN", server.slug, silence)
                        return ServerStatus.UNKNOWN
                return ServerStatus.ONLINE
            else:
                state.consecutive_failures += 1
                logger.warning("%s: healthcheck selhal (%d/%d)",
                               server.slug, state.consecutive_failures, CRASH_THRESHOLD)
                if state.consecutive_failures >= CRASH_THRESHOLD:
                    state.last_error = (
                        f"Proces zmizel po {state.consecutive_failures} selhání, "
                        f"tmux_alive={info.tmux_alive}"
                    )
                    return ServerStatus.CRASHED
                return ServerStatus.ONLINE

        if cur == ServerStatus.UNKNOWN:
            if info.pid and info.tmux_alive:
                state.consecutive_failures = 0
                return ServerStatus.ONLINE
            state.consecutive_failures += 1
            if state.consecutive_failures >= CRASH_THRESHOLD:
                state.last_error = "Přetrvávající UNKNOWN → CRASHED"
                return ServerStatus.CRASHED
            return ServerStatus.UNKNOWN

        return cur  # OFFLINE / CRASHED – nemění watchdog

    def _startup_confirmed(self, server):
        from apps.servers.adapters import get_adapter
        from apps.console.models import ConsoleLine
        adapter  = get_adapter(server.game_type)
        patterns = adapter.startup_patterns()
        if not patterns:
            return False
        recent = ConsoleLine.objects.filter(server=server).order_by("-timestamp")[:50]
        return any(adapter.is_startup_complete(line.line) for line in recent)

    def _on_transition(self, server, old_status, new_status, channel_layer):
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
        async_to_sync(channel_layer.group_send)(
            f"server.{server.id}.events",
            {"type": "audit.event", "event_type": "server.status.changed",
             "severity": severity, "message": msg, "timestamp": ts}
        )

        self._maybe_webhook(server, new_status)

        # Fáze 4: alert engine
        try:
            from apps.alerts.engine import check_status_alerts
            check_status_alerts(server, new_status)
        except Exception as exc:
            logger.warning("Alert engine chyba: %s", exc)

        if new_status == ServerStatus.OFFLINE:
            from apps.control.restart_handler import maybe_auto_restart
            maybe_auto_restart(server)

    def _maybe_webhook(self, server, new_status):
        if not server.webhook_url:
            return
        should = (
            (new_status == ServerStatus.CRASHED and server.webhook_on_crash) or
            (new_status == ServerStatus.ONLINE  and server.webhook_on_start)  or
            (new_status == ServerStatus.OFFLINE and server.webhook_on_stop)
        )
        if not should:
            return
        icons = {ServerStatus.CRASHED:"🔴", ServerStatus.ONLINE:"🟢", ServerStatus.OFFLINE:"⚫"}
        try:
            requests.post(
                server.webhook_url,
                json={"content": f"{icons.get(new_status,'🟡')} **{server.name}**: `{new_status}`"},
                timeout=5,
            ).raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Webhook selhal pro %s: %s", server.slug, exc)
