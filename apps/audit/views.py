"""
apps/audit/views.py

Stránka audit logu – přehled všech eventů pro server.
Podporuje filtrování podle severity a event_type.
"""
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.core.paginator import Paginator

from apps.servers.models import Server
from apps.audit.models import AuditEvent


@login_required
def audit_log(request, slug):
    server   = get_object_or_404(Server, slug=slug, is_active=True)
    severity = request.GET.get("severity", "")
    etype    = request.GET.get("type", "")

    qs = AuditEvent.objects.filter(server=server).select_related("user")
    if severity:
        qs = qs.filter(severity=severity)
    if etype:
        qs = qs.filter(event_type__icontains=etype)

    paginator = Paginator(qs, 50)
    page      = paginator.get_page(request.GET.get("page", 1))

    # Distinct event types pro filter dropdown
    event_types = (
        AuditEvent.objects
        .filter(server=server)
        .values_list("event_type", flat=True)
        .distinct()
        .order_by("event_type")
    )

    return render(request, "audit/audit_log.html", {
        "server":      server,
        "page":        page,
        "severity":    severity,
        "etype":       etype,
        "event_types": event_types,
    })


@login_required
def audit_log_api(request, slug):
    """JSON endpoint pro live fetch posledních eventů."""
    server = get_object_or_404(Server, slug=slug, is_active=True)
    since  = request.GET.get("since")   # ISO timestamp

    qs = AuditEvent.objects.filter(server=server).order_by("-timestamp")[:50]
    if since:
        from django.utils.dateparse import parse_datetime
        since_dt = parse_datetime(since)
        if since_dt:
            qs = AuditEvent.objects.filter(
                server=server, timestamp__gt=since_dt
            ).order_by("-timestamp")[:50]

    events = [
        {
            "id":         e.id,
            "timestamp":  e.timestamp.isoformat(),
            "event_type": e.event_type,
            "severity":   e.severity,
            "user":       e.user.username if e.user else None,
            "message":    e.message,
        }
        for e in qs
    ]
    return JsonResponse({"events": events})
