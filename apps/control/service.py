"""
apps/control/service.py

Orchestrace akcí start / stop / restart / force-stop.
View funkce NIKDY nevolají tmux přímo – vše jde přes tuto vrstvu.

Locking: Redis lock na úrovni serveru zabrání race condition
při dvojkliku nebo souběžném requestu.
"""
import uuid
import logging
import threading
from contextlib import contextmanager

from django.utils import timezone
from django.core.cache import cache

from apps.servers.models import Server, ServerStatus, ServerProcessState, GameType
from apps.audit.models import AuditEvent
from apps.console.models import CommandHistory
from apps.control.startup_probe import is_startup_ready

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = 30
MINECRAFT_TYPES = {GameType.MINECRAFT_JAVA, GameType.MINECRAFT_BEDROCK}


def _require_control_permission(server: Server, user):
    if user is None:
        return
    from apps.users.permissions import can_control_server
    if not can_control_server(user, server):
        raise PermissionError("Přístup odepřen.")


def _get_backend():
    """Vrátí tmux backend pokud je tmux dostupný, jinak subprocess backend."""
    import shutil
    if shutil.which("tmux"):
        from apps.control.backends.tmux import LocalTmuxProcessBackend
        return LocalTmuxProcessBackend()
    from apps.control.backends.subprocess_backend import SubprocessBackend
    return SubprocessBackend()


# Singleton backend – detekován jednou při importu
backend = _get_backend()

# Unifikovaná výjimka – funguje pro oba backendy
class BackendError(Exception):
    pass


def _wrap_backend_call(fn, *args, **kwargs):
    """Zavolá backend funkci a přeloží specifické výjimky na BackendError."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise BackendError(str(exc)) from exc


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


def _resolve_runtime_status(server: Server, info) -> str:
    if server.status == ServerStatus.STARTING and info.status != ServerStatus.CRASHED:
        return ServerStatus.STARTING
    if server.status == ServerStatus.STOPPING:
        return ServerStatus.STOPPING if info.pid else ServerStatus.OFFLINE
    return info.status


def _sync_runtime_status(server: Server):
    """Srovná uložený stav serveru s aktuálním stavem backendu."""
    try:
        info = backend.get_process_info(server)
    except Exception:
        return None

    actual_status = _resolve_runtime_status(server, info)

    if actual_status == server.status:
        return info

    server.status = actual_status
    server.last_seen_at = timezone.now()
    server.save(update_fields=["status", "last_seen_at", "updated_at"])

    state = _ensure_process_state(server)
    state.status = actual_status
    state.pid = info.pid
    state.save(update_fields=["status", "pid"])
    return info


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


def _set_desired_running(server: Server, should_run: bool):
    if server.desired_running == should_run:
        return
    server.desired_running = should_run
    server.save(update_fields=["desired_running", "updated_at"])


def _dispatch_console_command(server: Server, command: str, user=None, source: str = "action_button"):
    cmd = _record_command(server, command, user, source)
    try:
        _wrap_backend_call(backend.send_command, server, command)
        _mark_dispatched(cmd)
        return cmd
    except BackendError as exc:
        _mark_failed(cmd, str(exc))
        raise


def _send_command_via_rcon(server: Server, command: str) -> str:
    from apps.control.backends.rcon import rcon_command

    host = (server.rcon_host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(server.rcon_port or 25575)
    password = (server.rcon_password or "").strip()
    if not server.rcon_enabled or not password:
        raise BackendError("Server není spuštěný nebo nemá stdin handle (restartuj server z panelu).")
    return rcon_command(host, port, password, command)


# ─────────────────────────────────────────────────────────────
# Startup watchdog (bez externího workeru)
# ─────────────────────────────────────────────────────────────

def _push_status_ws(server_id: str, status: str):
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        async_to_sync(layer.group_send)(
            f"server.{server_id}.status",
            {"type": "server.status", "status": status},
        )
    except Exception as e:
        logger.debug("[push_status_ws] %s", e)


def _push_console_state_ws(server: Server):
    try:
        get_state = getattr(backend, "get_console_state", None)
        if get_state is None:
            return
        state = get_state(server) or {}
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        async_to_sync(layer.group_send)(
            f"server.{server.id}.console",
            {
                "type": "console.state",
                "available": state.get("available", False),
                "can_write": state.get("can_write", False),
                "message": state.get("message", ""),
            },
        )
    except Exception as e:
        logger.debug("[push_console_state_ws] %s", e)

def _watch_startup(server_pk, server_slug, max_wait=120, interval=5):
    """Daemon thread: hlídá startup a přepne status na ONLINE/CRASHED."""
    import time
    import django
    django.setup.__module__  # ensure Django is ready (already is in threads)
    from django.db import connection as _conn

    for _ in range(max_wait // interval):
        time.sleep(interval)
        try:
            _conn.close()  # nový connection z poolu pro tento thread
            srv = Server.objects.get(pk=server_pk)
            if srv.status not in (ServerStatus.STARTING,):
                break  # jiná akce mezitím změnila stav
            info = backend.get_process_info(srv)
            if info.pid and is_startup_ready(srv):
                _set_server_status(srv, ServerStatus.ONLINE)
                state = _ensure_process_state(srv)
                state.status = ServerStatus.ONLINE
                state.pid    = info.pid
                state.save(update_fields=["status", "pid"])
                _push_status_ws(str(srv.id), ServerStatus.ONLINE)
                _push_console_state_ws(srv)
                break
            elif info.status == ServerStatus.CRASHED:
                _set_server_status(srv, ServerStatus.CRASHED)
                _push_status_ws(str(srv.id), ServerStatus.CRASHED)
                _push_console_state_ws(srv)
                break
        except Exception as e:
            logger.debug("[watch_startup %s] %s", server_slug, e)


def _watch_shutdown(server_pk, server_slug, max_wait=60, interval=3):
    """Daemon thread: čeká na zastavení procesu a přepne status na OFFLINE."""
    import time
    from django.db import connection as _conn

    for _ in range(max_wait // interval):
        time.sleep(interval)
        try:
            _conn.close()
            srv = Server.objects.get(pk=server_pk)
            if srv.status not in (ServerStatus.STOPPING,):
                break
            info = backend.get_process_info(srv)
            if info.status in (ServerStatus.OFFLINE, ServerStatus.CRASHED):
                _set_server_status(srv, ServerStatus.OFFLINE)
                state = _ensure_process_state(srv)
                state.status     = ServerStatus.OFFLINE
                state.stopped_at = timezone.now()
                state.pid        = None
                state.save(update_fields=["status", "stopped_at", "pid"])
                _push_status_ws(str(srv.id), ServerStatus.OFFLINE)
                _push_console_state_ws(srv)
                from apps.control.restart_handler import maybe_auto_restart
                maybe_auto_restart(srv)
                break
        except Exception as e:
            logger.debug("[watch_shutdown %s] %s", server_slug, e)
    else:
        # Timeout — force kill
        try:
            _conn.close()
            srv = Server.objects.get(pk=server_pk)
            if srv.status == ServerStatus.STOPPING:
                backend.kill_server(srv)
                _set_server_status(srv, ServerStatus.OFFLINE)
                _push_status_ws(str(srv.id), ServerStatus.OFFLINE)
                _push_console_state_ws(srv)
                from apps.control.restart_handler import maybe_auto_restart
                maybe_auto_restart(srv)
        except Exception as e:
            logger.debug("[watch_shutdown force %s] %s", server_slug, e)


def _force_kill_background(server_pk, server_slug):
    from django.db import connection as _conn

    try:
        _conn.close()
        srv = Server.objects.get(pk=server_pk)
        _wrap_backend_call(backend.kill_server, srv)

        state = _ensure_process_state(srv)
        state.pid = None
        state.stopped_at = timezone.now()
        state.status = ServerStatus.OFFLINE
        state.consecutive_failures = 0
        state.last_error = ""
        state.save(update_fields=["pid", "stopped_at", "status", "consecutive_failures", "last_error"])
    except Exception as exc:
        logger.error("[force_kill %s] %s", server_slug, exc)


# ─────────────────────────────────────────────────────────────
# Akce
# ─────────────────────────────────────────────────────────────

def start_server(server: Server, user=None) -> dict:
    """
    Spustí server.
    Vrací {"ok": True/False, "message": str}
    """
    try:
        _require_control_permission(server, user)
    except PermissionError as exc:
        return {"ok": False, "message": str(exc)}

    with server_action_lock(server):
        _sync_runtime_status(server)
        if server.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            return {"ok": False, "message": f"Server je již ve stavu {server.status}."}

        cmd = _record_command(server, f"[START] {server.start_command}", user, "action_button")
        _audit(server, "server.start.requested", f"Spuštění požadováno uživatelem {user}", user=user)

        try:
            _wrap_backend_call(backend.start_server, server)
            _mark_dispatched(cmd)
        except BackendError as exc:
            _mark_failed(cmd, str(exc))
            _audit(server, "server.start.failed", str(exc), severity="error")
            return {"ok": False, "message": str(exc)}

        _set_desired_running(server, True)

        state = _ensure_process_state(server)
        state.started_at         = timezone.now()
        state.status             = ServerStatus.STARTING
        state.consecutive_failures = 0
        state.last_error         = ""
        state.save()

        _set_server_status(server, ServerStatus.STARTING)
        _push_status_ws(str(server.id), ServerStatus.STARTING)
        _push_console_state_ws(server)
        backend_name = type(backend).__name__
        _audit(server, "server.start.dispatched", f"Start předán backendu {backend_name}", user=user)

        # Background thread: přepne status na ONLINE jakmile process běží
        import threading
        server_pk = server.pk
        server_slug = server.slug
        threading.Thread(
            target=_watch_startup, args=(server_pk, server_slug),
            daemon=True, name=f"startup-watch-{server_slug}",
        ).start()

        return {"ok": True, "message": "Server startuje…"}


def stop_server(server: Server, user=None) -> dict:
    try:
        _require_control_permission(server, user)
    except PermissionError as exc:
        return {"ok": False, "message": str(exc)}

    with server_action_lock(server):
        if server.status == ServerStatus.OFFLINE:
            return {"ok": False, "message": "Server je již offline."}
        if server.status == ServerStatus.STOPPING:
            return {"ok": False, "message": "Server se již zastavuje."}

        stop_cmd = server.stop_command or "stop"
        cmd = _record_command(server, f"[STOP] {stop_cmd}", user, "action_button")
        _audit(server, "server.stop.requested", f"Zastavení požadováno uživatelem {user}", user=user)

        try:
            _set_desired_running(server, False)
            _wrap_backend_call(backend.stop_server, server)
            _mark_dispatched(cmd)
        except BackendError as exc:
            _mark_failed(cmd, str(exc))
            _audit(server, "server.stop.failed", str(exc), severity="error")
            return {"ok": False, "message": str(exc)}

        state = _ensure_process_state(server)
        state.last_command_at = timezone.now()
        state.save(update_fields=["last_command_at"])

        _set_server_status(server, ServerStatus.STOPPING)
        _push_status_ws(str(server.id), ServerStatus.STOPPING)
        _push_console_state_ws(server)
        _audit(server, "server.stop.dispatched", "Stop command odeslán", user=user)

        import threading
        threading.Thread(
            target=_watch_shutdown, args=(server.pk, server.slug),
            daemon=True, name=f"shutdown-watch-{server.slug}",
        ).start()

        return {"ok": True, "message": "Server se zastavuje…"}


def restart_server(server: Server, user=None) -> dict:
    try:
        _require_control_permission(server, user)
    except PermissionError as exc:
        return {"ok": False, "message": str(exc)}

    with server_action_lock(server):
        if server.status == ServerStatus.OFFLINE:
            return {"ok": False, "message": "Server není spuštěn."}
        if server.status == ServerStatus.STOPPING:
            return {"ok": False, "message": "Server se právě zastavuje."}

        _audit(server, "server.restart.requested", f"Restart požadován uživatelem {user}", user=user)
        _set_desired_running(server, True)

        from django.core.cache import cache
        cache.set(f"pending_restart:{server.id}", True, timeout=300)

        if server.game_type in MINECRAFT_TYPES:
            try:
                _dispatch_console_command(server, "save-all", user, "action_button")
                _audit(server, "server.restart.save_all", "Příkaz save-all odeslán před restartem.", user=user)
            except BackendError as exc:
                cache.delete(f"pending_restart:{server.id}")
                _audit(server, "server.restart.failed", f"save-all selhal: {exc}", severity="error", user=user)
                return {"ok": False, "message": f"save-all selhal: {exc}"}

        stop_cmd = server.stop_command or "stop"
        try:
            _dispatch_console_command(server, stop_cmd, user, "action_button")
        except BackendError as exc:
            cache.delete(f"pending_restart:{server.id}")
            _audit(server, "server.restart.failed", str(exc), severity="error")
            return {"ok": False, "message": f"Stop selhal: {exc}"}

        _set_server_status(server, ServerStatus.STOPPING)
        _push_status_ws(str(server.id), ServerStatus.STOPPING)
        _push_console_state_ws(server)
        _audit(server, "server.restart.stopping", "Stop command odeslán, čekám na shutdown…", user=user)

        # Watchdog se postará o přechod STOPPING → OFFLINE → STARTING po dokončení
        # Uložíme příznak "po offline spusť znovu"
        state = _ensure_process_state(server)
        state.last_command_at = timezone.now()
        state.save(update_fields=["last_command_at"])

        import threading
        threading.Thread(
            target=_watch_shutdown, args=(server.pk, server.slug),
            daemon=True, name=f"restart-watch-{server.slug}",
        ).start()

        return {"ok": True, "message": "Restart zahájen – server se zastavuje…"}


def force_stop_server(server: Server, user=None) -> dict:
    try:
        _require_control_permission(server, user)
    except PermissionError as exc:
        return {"ok": False, "message": str(exc)}

    with server_action_lock(server):
        if server.status == ServerStatus.OFFLINE:
            return {"ok": False, "message": "Server je již offline."}

        cmd = _record_command(server, "[FORCE-STOP] kill-session", user, "action_button")
        _audit(server, "server.force_stop.requested",
               f"FORCE STOP požadován uživatelem {user}",
               user=user, severity="warning")

        _mark_dispatched(cmd)

        cache.delete(f"pending_restart:{server.id}")
        _set_desired_running(server, False)

        state = _ensure_process_state(server)
        state.pid        = None
        state.stopped_at = timezone.now()
        state.status     = ServerStatus.OFFLINE
        state.consecutive_failures = 0
        state.last_error = ""
        state.save()

        _set_server_status(server, ServerStatus.OFFLINE)
        _push_status_ws(str(server.id), ServerStatus.OFFLINE)
        _push_console_state_ws(server)
        _audit(server, "server.force_stop.done",
               "Server násilně zabit.", user=user, severity="warning")

        threading.Thread(
            target=_force_kill_background,
            args=(server.pk, server.slug),
            daemon=True,
            name=f"force-kill-{server.slug}",
        ).start()

        return {"ok": True, "message": "Server se násilně ukončuje."}


def send_console_command(server: Server, command: str, user=None) -> dict:
    try:
        _require_control_permission(server, user)
    except PermissionError as exc:
        return {"ok": False, "message": str(exc)}

    if server.status not in (ServerStatus.ONLINE, ServerStatus.STARTING):
        return {"ok": False, "message": "Server není spuštěný."}
    if not command.strip():
        return {"ok": False, "message": "Prázdný příkaz."}
    # Základní ochrana – nepovolíme ; && || pro command injection
    for forbidden in (";", "&&", "||", "`", "$("):
        if forbidden in command:
            return {"ok": False, "message": f"Zakázaný znak v příkazu: {forbidden}"}

    cmd = _record_command(server, command, user, "web_console")

    try:
        _wrap_backend_call(backend.send_command, server, command)
        _mark_dispatched(cmd)
        return {"ok": True, "message": "Příkaz odeslán.", "correlation_id": cmd.correlation_id}
    except BackendError as exc:
        try:
            response = _send_command_via_rcon(server, command)
            _mark_dispatched(cmd)
            message = "Příkaz odeslán přes RCON."
            if response.strip():
                message += f" {response.strip()}"
            return {"ok": True, "message": message, "correlation_id": cmd.correlation_id, "transport": "rcon"}
        except Exception:
            _mark_failed(cmd, str(exc))
            return {"ok": False, "message": str(exc)}
