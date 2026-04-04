"""
apps/servers/player_tracker.py

Zpracovava radky konzole a aktualizuje player count + PlayerSession zaznamy.
Volano z run_console_tail pro kazdy novy radek.
"""
import logging
from django.utils import timezone
from apps.servers.models import Server, PlayerSession
from apps.servers.adapters import get_adapter

logger = logging.getLogger(__name__)


def process_line_for_players(server: Server, line: str):
    adapter = get_adapter(server.game_type)
    event = adapter.extract_player_events(line)
    if event:
        _handle_event(server, event)
    count = adapter.extract_player_count(line)
    if count is not None:
        _update_count(server, count)
    tps = adapter.extract_tps(line)
    if tps is not None:
        _update_tps(server, tps)


def _handle_event(server, event):
    from apps.audit.models import AuditEvent
    now = timezone.now()
    action = "se pripojil" if event.event_type == "join" else "se odpojil"
    logger.info("[%s] %s %s", server.slug, event.player_name, action)

    AuditEvent.objects.create(
        server=server,
        event_type=f"player.{event.event_type}",
        severity="info",
        message=f"Hrac {event.player_name} {action}",
        payload_json={"player": event.player_name, "event": event.event_type},
    )

    if event.event_type == "join":
        PlayerSession.objects.create(
            server=server, player_name=event.player_name, joined_at=now
        )
        _delta_count(server, +1)
    elif event.event_type == "leave":
        session = (
            PlayerSession.objects
            .filter(server=server, player_name=event.player_name, left_at=None)
            .order_by("-joined_at").first()
        )
        if session:
            session.left_at = now
            session.duration_seconds = int((now - session.joined_at).total_seconds())
            session.save(update_fields=["left_at", "duration_seconds"])
        _delta_count(server, -1)


def _delta_count(server, delta):
    try:
        state = server.process_state
        state.last_player_count = max(0, state.last_player_count + delta)
        state.save(update_fields=["last_player_count"])
    except Exception as e:
        logger.warning("player_count delta error: %s", e)


def _update_count(server, count):
    try:
        state = server.process_state
        if state.last_player_count != count:
            state.last_player_count = count
            state.save(update_fields=["last_player_count"])
    except Exception:
        pass


def _update_tps(server, tps):
    try:
        from apps.metrics.models import MetricSample
        last = MetricSample.objects.filter(server=server).order_by("-timestamp").first()
        if last:
            last.tps = tps
            last.save(update_fields=["tps"])
    except Exception:
        pass
