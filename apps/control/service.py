"""
apps/control/service.py

Orchestrace akcí start / stop / restart / force-stop.
View funkce NIKDY nevolají tmux přímo – vše jde přes tuto vrstvu.

Locking: Redis lock na úrovni serveru zabrání race condition
při dvojkliku nebo souběžném requestu.
"""
import uuid
import logging
from contextlib import contextmanager

from django.utils import timezone
from django.core.cache import cache

from apps.servers.models import Server, ServerStatus, ServerProcessState
from apps.audit.models import AuditEvent
from apps.console.models import CommandHistory
from apps.control.backends.tmux import LocalTmuxProcessBackend, TmuxError

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = 30   # sekund – déle než longest expected action
backend      = LocalTmuxProcessBackend()


# ─────────────────────────────────────────────────────────────
# Lock
# ─────────────────────────────────────────────────────────────

class ServerLockError(Exception):
    pass


@contextmanager
def server_action_lock(server: Server):
    """
    Redis-based distributed lock pro akce na jednom serveru.
    Raises ServerLockError pokud je server právě blokován jinou akcí.
    """
    lock_key = f"server_action_lock:{server.id}"
    lock_val = str(uuid.uuid4())

    acquired = cache.add(lock_key, lock_val, timeout=LOCK_TIMEOUT)
    if not acquired:
        raise ServerLockError(f"Server '{server.name}' je právě obsazen jinou akcí. Zkus to za chvíli.")

    try:
        yield
    finally:
        # Uvolni jen pokud máme náš lock (ochrana před uvolněním cizího locku)
        if cache.get(lock_key) == lock_val:
            cache.delete(lock_key)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _ensure_process_state(server: Server) -> ServerProcessState:
    state, _ = ServerProcessState.objects.get_or_create(server=server)
    return state


def _set_server_status(server: Server, status: str):
    server.status     = status
    server.last_seen_at = timezone.now()
    server.save(update_fields=["status", "last_seen_at", "updated_at"])


def _audit(server: Server, event_type: str, message: str,
           user=None, severity: str = "info", payload: dict = None):
    AuditEvent.objects.create(
        server=server,
        event_type=event_type,
        severity=severity,
        user=user,
        message=message,
        payload_json=payload or {},
    )


def _record_command(server: Server, command: str, user=None,
                    source: str = "action_button") -> CommandHistory:
    return CommandHistory.objects.create(
        server=server,
        user=user,
        command=command,
        source=source,
        result_status=CommandHistory.ResultStatus.ACCEPTED,
        accepted_at=timezone.now(),
        correlation_id=str(uuid.uuid4()),
    )


def _mark_dispatched(cmd: CommandHistory):
    cmd.result_status = CommandHistory.ResultStatus.DISPATCHED
    cmd.dispatched_at = timezone.now()
    cmd.save(update_fields=["result_status", "dispatched_at"])


def _mark_failed(cmd: CommandHistory, message: str):
    cmd.result_status  = CommandHistory.ResultStatus.FAILED
    cmd.result_message = message
    cmd.save(update_fields=["result_status", "result_message"])


# ─────────────────────────────────────────────────────────────
# Akce
# ─────────────────────────────────────────────────────────────

def start_server(server: Server, user=None) -> dict:
    """
    Spustí server.
    Vrací {"ok": True/False, "message": str}
    """
    with server_action_lock(server):
        if server.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            return {"ok": False, "message": f"Server je již ve stavu {server.status}."}

        cmd = _record_command(server, f"[START] {server.start_command}", user, "action_button")
        _audit(server, "server.start.requested", f"Spuštění požadováno uživatelem {user}", user=user)

        try:
            backend.start_server(server)
            _mark_dispatched(cmd)
        except TmuxError as exc:
            _mark_failed(cmd, str(exc))
            _audit(server, "server.start.failed", str(exc), severity="error")
            return {"ok": False, "message": str(exc)}

        state = _ensure_process_state(server)
        state.started_at         = timezone.now()
        state.status             = ServerStatus.STARTING
        state.consecutive_failures = 0
        state.last_error         = ""
        state.save()

        _set_server_status(server, ServerStatus.STARTING)
        _audit(server, "server.start.dispatched", "tmux session spuštěna", user=user)

        return {"ok": True, "message": "Server startuje…"}


def stop_server(server: Server, user=None) -> dict:
    with server_action_lock(server):
        if server.status == ServerStatus.OFFLINE:
            return {"ok": False, "message": "Server je již offline."}
        if server.status == ServerStatus.STOPPING:
            return {"ok": False, "message": "Server se již zastavuje."}

        stop_cmd = server.stop_command or "stop"
        cmd = _record_command(server, f"[STOP] {stop_cmd}", user, "action_button")
        _audit(server, "server.stop.requested", f"Zastavení požadováno uživatelem {user}", user=user)

        try:
            backend.stop_server(server)
            _mark_dispatched(cmd)
        except TmuxError as exc:
            _mark_failed(cmd, str(exc))
            _audit(server, "server.stop.failed", str(exc), severity="error")
            return {"ok": False, "message": str(exc)}

        state = _ensure_process_state(server)
        state.last_command_at = timezone.now()
        state.save(update_fields=["last_command_at"])

        _set_server_status(server, ServerStatus.STOPPING)
        _audit(server, "server.stop.dispatched", "Stop command odeslán", user=user)

        return {"ok": True, "message": "Server se zastavuje…"}


def restart_server(server: Server, user=None) -> dict:
    with server_action_lock(server):
        if server.status == ServerStatus.OFFLINE:
            return {"ok": False, "message": "Server není spuštěn."}

        _audit(server, "server.restart.requested", f"Restart požadován uživatelem {user}", user=user)

        # Stop
        stop_cmd = server.stop_command or "stop"
        try:
            backend.stop_server(server)
        except TmuxError as exc:
            _audit(server, "server.restart.failed", str(exc), severity="error")
            return {"ok": False, "message": f"Stop selhal: {exc}"}

        _set_server_status(server, ServerStatus.STOPPING)
        _audit(server, "server.restart.stopping", "Stop command odeslán, čekám na shutdown…", user=user)

        # Watchdog se postará o přechod STOPPING → OFFLINE → STARTING po dokončení
        # Uložíme příznak "po offline spusť znovu"
        state = _ensure_process_state(server)
        state.last_command_at = timezone.now()
        state.save(update_fields=["last_command_at"])

        # Zjednodušený přístup: flag v cache pro watchdog
        from django.core.cache import cache
        cache.set(f"pending_restart:{server.id}", True, timeout=120)

        return {"ok": True, "message": "Restart zahájen – server se zastavuje…"}


def force_stop_server(server: Server, user=None) -> dict:
    with server_action_lock(server):
        if server.status == ServerStatus.OFFLINE:
            return {"ok": False, "message": "Server je již offline."}

        cmd = _record_command(server, "[FORCE-STOP] kill-session", user, "action_button")
        _audit(server, "server.force_stop.requested",
               f"FORCE STOP požadován uživatelem {user}",
               user=user, severity="warning")

        try:
            backend.kill_server(server)
            _mark_dispatched(cmd)
        except TmuxError as exc:
            _mark_failed(cmd, str(exc))
            return {"ok": False, "message": str(exc)}

        state = _ensure_process_state(server)
        state.pid        = None
        state.stopped_at = timezone.now()
        state.status     = ServerStatus.OFFLINE
        state.save()

        _set_server_status(server, ServerStatus.OFFLINE)
        _audit(server, "server.force_stop.done",
               "Server násilně zabit.", user=user, severity="warning")

        return {"ok": True, "message": "Server byl násilně zastaven."}


def send_console_command(server: Server, command: str, user=None) -> dict:
    if server.status != ServerStatus.ONLINE:
        return {"ok": False, "message": "Server není online."}
    if not command.strip():
        return {"ok": False, "message": "Prázdný příkaz."}
    # Základní ochrana – nepovolíme ; && || pro command injection
    for forbidden in (";", "&&", "||", "`", "$("):
        if forbidden in command:
            return {"ok": False, "message": f"Zakázaný znak v příkazu: {forbidden}"}

    cmd = _record_command(server, command, user, "web_console")

    try:
        backend.send_command(server, command)
        _mark_dispatched(cmd)
    except TmuxError as exc:
        _mark_failed(cmd, str(exc))
        return {"ok": False, "message": str(exc)}

    return {"ok": True, "message": "Příkaz odeslán.", "correlation_id": cmd.correlation_id}
