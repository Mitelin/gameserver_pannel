"""
apps/dashboard/players_view.py

Stránka s historií hráčů pro jeden server.
GET /servers/<slug>/players/
"""
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse

from apps.servers.models import Server
from apps.servers.models import PlayerSession


@login_required
def player_history(request, slug):
    server   = get_object_or_404(Server, slug=slug, is_active=True)
    player   = request.GET.get("player", "")
    qs       = PlayerSession.objects.filter(server=server).select_related()

    if player:
        qs = qs.filter(player_name__icontains=player)

    paginator = Paginator(qs, 50)
    page      = paginator.get_page(request.GET.get("page", 1))

    # Top hráči (nejvíce sessií)
    from django.db.models import Count, Sum
    top_players = (
        PlayerSession.objects
        .filter(server=server)
        .values("player_name")
        .annotate(sessions=Count("id"), total_seconds=Sum("duration_seconds"))
        .order_by("-sessions")[:10]
    )

    # Aktuálně online (session bez left_at)
    online_now = list(
        PlayerSession.objects
        .filter(server=server, left_at=None)
        .values_list("player_name", flat=True)
    )

    return render(request, "dashboard/player_history.html", {
        "server":      server,
        "page":        page,
        "player":      player,
        "top_players": top_players,
        "online_now":  online_now,
    })
