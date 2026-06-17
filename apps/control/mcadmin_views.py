"""
apps/control/mcadmin_views.py

Views pro Minecraft whitelist a ban správu.
"""
import logging
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.servers.models import Server, GameType

logger = logging.getLogger(__name__)

MINECRAFT_TYPES = {GameType.MINECRAFT_JAVA, GameType.MINECRAFT_BEDROCK}


def _require_mc_staff(request, server):
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "message": "Vyžaduje admin oprávnění."}, status=403)
    if server.game_type not in MINECRAFT_TYPES:
        return JsonResponse({"ok": False, "message": "Pouze pro Minecraft servery."}, status=400)
    return None


@login_required
def mcadmin_view(request, slug):
    """Zobrazí whitelist + ban přehled."""
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not request.user.is_staff:
        from django.http import Http404
        raise Http404

    from .mcadmin import whitelist_list, ban_list, ip_ban_list
    return render(request, "control/mcadmin.html", {
        "server":    server,
        "whitelist": whitelist_list(server),
        "ban_list":  ban_list(server),
        "ip_ban_list": ip_ban_list(server),
        "is_mc":     server.game_type in MINECRAFT_TYPES,
    })


@login_required
@require_POST
def whitelist_add_view(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    err = _require_mc_staff(request, server)
    if err:
        return err
    name = request.POST.get("player", "").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "Zadej jméno hráče."})
    from .mcadmin import whitelist_add
    ok, msg = whitelist_add(server, name)
    return JsonResponse({"ok": ok, "message": msg})


@login_required
@require_POST
def whitelist_remove_view(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    err = _require_mc_staff(request, server)
    if err:
        return err
    name = request.POST.get("player", "").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "Zadej jméno hráče."})
    from .mcadmin import whitelist_remove
    ok, msg = whitelist_remove(server, name)
    return JsonResponse({"ok": ok, "message": msg})


@login_required
@require_POST
def ban_add_view(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    err = _require_mc_staff(request, server)
    if err:
        return err
    name   = request.POST.get("player", "").strip()
    reason = request.POST.get("reason", "Banned by admin").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "Zadej jméno hráče."})
    from .mcadmin import ban_add
    ok, msg = ban_add(server, name, reason)
    return JsonResponse({"ok": ok, "message": msg})


@login_required
@require_POST
def ban_remove_view(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    err = _require_mc_staff(request, server)
    if err:
        return err
    name = request.POST.get("player", "").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "Zadej jméno hráče."})
    from .mcadmin import ban_remove
    ok, msg = ban_remove(server, name)
    return JsonResponse({"ok": ok, "message": msg})


@login_required
@require_POST
def ip_ban_add_view(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    err = _require_mc_staff(request, server)
    if err:
        return err
    ip_address = request.POST.get("ip", "").strip()
    reason = request.POST.get("reason", "Banned by admin").strip()
    if not ip_address:
        return JsonResponse({"ok": False, "message": "Zadej IP adresu."})
    from .mcadmin import ip_ban_add
    ok, msg = ip_ban_add(server, ip_address, reason)
    return JsonResponse({"ok": ok, "message": msg})


@login_required
@require_POST
def ip_ban_remove_view(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    err = _require_mc_staff(request, server)
    if err:
        return err
    ip_address = request.POST.get("ip", "").strip()
    if not ip_address:
        return JsonResponse({"ok": False, "message": "Zadej IP adresu."})
    from .mcadmin import ip_ban_remove
    ok, msg = ip_ban_remove(server, ip_address)
    return JsonResponse({"ok": ok, "message": msg})
