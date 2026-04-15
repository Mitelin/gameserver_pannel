"""
apps/dashboard/views.py  (fáze 4 + fáze 2 permissions)

Multi-server overview dashboard + server detail.
"""
import logging
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse

from apps.servers.models import Server
from apps.console.models import ConsoleLine, CommandHistory
from apps.audit.models import AuditEvent
from apps.users.permissions import accessible_servers, can_view_server, get_profile

logger = logging.getLogger(__name__)
INITIAL_CONSOLE_LINES = 100


@login_required
def server_list(request):
    """Multi-server overview – hlavní dashboard."""
    from apps.servers.models import ServerStatus

    base_qs = Server.objects.filter(is_active=True).select_related("process_state")
    servers  = accessible_servers(request.user, base_qs).order_by("name")

    total   = servers.count()
    online  = servers.filter(status=ServerStatus.ONLINE).count()
    crashed = servers.filter(status=ServerStatus.CRASHED).count()
    offline = servers.filter(status=ServerStatus.OFFLINE).count()

    profile = get_profile(request.user)

    ctx = {
        "servers": servers,
        "stats": {
            "total":   total,
            "online":  online,
            "crashed": crashed,
            "offline": offline,
        },
        "profile": profile,
    }
    return render(request, "dashboard/server_list.html", ctx)


@login_required
def server_detail(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied

    try:
        process_state = server.process_state
    except Exception:
        process_state = None

    initial_lines = list(
        ConsoleLine.objects.filter(server=server).order_by("-timestamp")[:INITIAL_CONSOLE_LINES]
    )[::-1]

    recent_commands = CommandHistory.objects.filter(server=server)[:20]
    recent_events   = AuditEvent.objects.filter(server=server)[:20]

    # Alert rules pro tento server
    from apps.alerts.models import AlertRule
    alert_rules = AlertRule.objects.filter(server=server, is_active=True)

    ctx = {
        "server":        server,
        "process_state": process_state,
        "initial_lines": initial_lines,
        "recent_commands": recent_commands,
        "recent_events": recent_events,
        "alert_rules":   alert_rules,
        "ws_url":        f"/ws/servers/{server.slug}/",
        "rcon_enabled":  server.rcon_enabled,
    }
    return render(request, "dashboard/server_detail.html", ctx)


@login_required
def server_status_api(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied
    try:
        ps = server.process_state
        return JsonResponse({
            "status":       server.status,
            "pid":          ps.pid,
            "cpu_percent":  ps.cpu_percent_last,
            "ram_bytes":    ps.rss_bytes_last,
            "players":      ps.last_player_count,
            "threads":      ps.thread_count_last,
            "last_seen_at": server.last_seen_at.isoformat() if server.last_seen_at else None,
        })
    except Exception:
        return JsonResponse({"status": server.status})
