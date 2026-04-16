"""
apps/servers/views.py  (fáze 4 + fáze 5)

Server management views:
  - server_create      GET/POST  /servers/new/
  - server_edit        GET/POST  /servers/<slug>/edit/
  - server_delete      POST      /servers/<slug>/delete/
  - server_history     GET       /servers/<slug>/history/
  - properties_editor  GET/POST  /servers/<slug>/properties/
  - profile_list       GET       /servers/<slug>/profiles/
  - profile_create     GET/POST  /servers/<slug>/profiles/new/
  - profile_edit       GET/POST  /servers/<slug>/profiles/<pk>/edit/
  - profile_delete     POST      /servers/<slug>/profiles/<pk>/delete/
  - profile_activate   POST      /servers/<slug>/profiles/<pk>/activate/
"""
import logging
from pathlib import Path

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.views.decorators.http import require_POST

from apps.users.permissions import can_edit_server_config, is_admin_or_above
from .models import Server, StartProfile, GameType
from .forms import ServerForm, StartProfileForm

logger = logging.getLogger(__name__)

_MINECRAFT_TYPES = {GameType.MINECRAFT_JAVA, GameType.MINECRAFT_BEDROCK}

# Pole která sledujeme pro config change history
_TRACKED_FIELDS = [
    "name", "game_type", "is_active",
    "working_directory", "start_command", "stop_command",
    "tmux_session_name", "log_file_path",
    "expected_startup_seconds", "expected_shutdown_seconds",
    "rcon_enabled", "rcon_host", "rcon_port",
    "webhook_url", "webhook_on_crash", "webhook_on_start", "webhook_on_stop",
]


def _log_config_changes(server: Server, old_values: dict, new_values: dict, user):
    """Zaloguje změny konfigurace serveru do AuditEvent."""
    changed = {}
    for field in _TRACKED_FIELDS:
        old = old_values.get(field)
        new = new_values.get(field)
        if old != new:
            # Skryj hesla v logu
            if "password" in field or "rcon_password" in field:
                old = "***" if old else ""
                new = "***" if new else ""
            changed[field] = {"old": str(old), "new": str(new)}

    if not changed:
        return

    from apps.audit.models import AuditEvent
    summary = ", ".join(changed.keys())
    AuditEvent.objects.create(
        server=server,
        event_type="server.config.changed",
        severity="info",
        user=user,
        message=f"Konfigurace upravena: {summary}",
        payload_json={"changed": changed},
    )
    logger.info("[%s] Config changed by %s: %s", server.slug, user, summary)


def _require_edit(request, server):
    """Vrátí HttpResponseForbidden pokud uživatel nemůže editovat config."""
    if not can_edit_server_config(request.user, server):
        return HttpResponseForbidden("Nemáš oprávnění upravovat konfiguraci tohoto serveru.")
    return None


# ── Server create ─────────────────────────────────────────────────────────────

@login_required
def server_create(request):
    if not is_admin_or_above(request.user):
        return HttpResponseForbidden("Vytváření serverů vyžaduje admin oprávnění.")

    if request.method == "POST":
        form = ServerForm(request.POST)
        if form.is_valid():
            server = form.save()
            # Vytvoř prázdný ProcessState
            from apps.servers.models import ServerProcessState
            ServerProcessState.objects.get_or_create(server=server)
            messages.success(request, f"Server '{server.name}' byl vytvořen.")
            return redirect(f"/servers/{server.slug}/edit/")
    else:
        form = ServerForm()

    return render(request, "servers/server_edit.html", {
        "form":      form,
        "server":    None,
        "is_create": True,
    })


# ── Server edit ───────────────────────────────────────────────────────────────

@login_required
def server_edit(request, slug):
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied

    if request.method == "POST":
        # Ulož staré hodnoty pro diff
        old_values = {f: getattr(server, f) for f in _TRACKED_FIELDS if hasattr(server, f)}
        form = ServerForm(request.POST, instance=server)
        if form.is_valid():
            form.save()
            new_values = {f: getattr(server, f) for f in _TRACKED_FIELDS if hasattr(server, f)}
            _log_config_changes(server, old_values, new_values, request.user)
            messages.success(request, "Konfigurace serveru byla uložena.")
            return redirect(f"/servers/{server.slug}/edit/")
    else:
        form = ServerForm(instance=server)

    profiles = server.start_profiles.all() if server.game_type in _MINECRAFT_TYPES else None

    return render(request, "servers/server_edit.html", {
        "form":         form,
        "server":       server,
        "is_create":    False,
        "profiles":     profiles,
        "is_minecraft": server.game_type in _MINECRAFT_TYPES,
    })


# ── Server delete ─────────────────────────────────────────────────────────────

@login_required
@require_POST
def server_delete(request, slug):
    server = get_object_or_404(Server, slug=slug)
    if not is_admin_or_above(request.user):
        return HttpResponseForbidden("Mazání serverů vyžaduje admin oprávnění.")
    name = server.name
    server.delete()
    messages.success(request, f"Server '{name}' byl smazán.")
    return redirect("/")


# ── Config change history ─────────────────────────────────────────────────────

@login_required
def server_history(request, slug):
    server = get_object_or_404(Server, slug=slug)
    if not can_edit_server_config(request.user, server):
        return HttpResponseForbidden("Nemáš přístup k historii konfigurace.")

    from apps.audit.models import AuditEvent
    events = AuditEvent.objects.filter(
        server=server,
        event_type="server.config.changed",
    ).select_related("user").order_by("-timestamp")[:100]

    return render(request, "servers/server_history.html", {
        "server": server,
        "events": events,
    })


# ── server.properties editor ──────────────────────────────────────────────────

@login_required
def properties_editor(request, slug):
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied

    if server.game_type not in _MINECRAFT_TYPES:
        messages.error(request, "Editor vlastností je dostupný jen pro Minecraft servery.")
        return redirect(f"/servers/{server.slug}/edit/")

    props_path = Path(server.working_directory) / "server.properties"
    error = None
    props = {}

    if request.method == "POST":
        # Ulož zpět – zachovej původní řádky (komentáře, pořadí)
        try:
            original_lines = props_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            original_lines = []

        new_lines = []
        seen_keys = set()
        for line in original_lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in request.POST:
                new_lines.append(f"{key}={request.POST[key]}\n")
                seen_keys.add(key)
            else:
                new_lines.append(line)

        # Přidej nové klíče z POST (které nebyly v originále)
        for key, val in request.POST.items():
            if key.startswith("_") or key == "csrfmiddlewaretoken":
                continue
            if key not in seen_keys:
                new_lines.append(f"{key}={val}\n")

        try:
            props_path.write_text("".join(new_lines), encoding="utf-8")
            messages.success(request, "server.properties bylo uloženo.")
        except OSError as e:
            messages.error(request, f"Chyba zápisu: {e}")

        return redirect(f"/servers/{slug}/properties/")

    # GET – načti soubor
    try:
        raw = props_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            props[key.strip()] = val.strip()
    except FileNotFoundError:
        error = f"Soubor {props_path} neexistuje. Spusť server alespoň jednou."
    except OSError as e:
        error = str(e)

    return render(request, "servers/properties_editor.html", {
        "server": server,
        "props":  props,
        "error":  error,
        "props_path": str(props_path),
    })


# ── Start profily ─────────────────────────────────────────────────────────────

@login_required
def profile_list(request, slug):
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied
    return render(request, "servers/profile_list.html", {
        "server":   server,
        "profiles": server.start_profiles.all(),
    })


@login_required
def profile_create(request, slug):
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied

    if request.method == "POST":
        form = StartProfileForm(request.POST)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.server = server
            profile.save()
            messages.success(request, f"Profil '{profile.name}' byl vytvořen.")
            return redirect(f"/servers/{slug}/profiles/")
    else:
        form = StartProfileForm()

    return render(request, "servers/profile_edit.html", {
        "form":      form,
        "server":    server,
        "profile":   None,
        "is_create": True,
    })


@login_required
def profile_edit(request, slug, pk):
    server  = get_object_or_404(Server, slug=slug)
    profile = get_object_or_404(StartProfile, pk=pk, server=server)
    denied  = _require_edit(request, server)
    if denied:
        return denied

    if request.method == "POST":
        form = StartProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            # Pokud je profil aktivní, aktualizuj start_command
            if profile.is_active:
                server.start_command = profile.build_command()
                server.save(update_fields=["start_command", "updated_at"])
            messages.success(request, "Profil byl uložen.")
            return redirect(f"/servers/{slug}/profiles/")
    else:
        form = StartProfileForm(instance=profile)

    return render(request, "servers/profile_edit.html", {
        "form":      form,
        "server":    server,
        "profile":   profile,
        "is_create": False,
        "preview":   profile.build_command(),
    })


@login_required
@require_POST
def profile_delete(request, slug, pk):
    server  = get_object_or_404(Server, slug=slug)
    profile = get_object_or_404(StartProfile, pk=pk, server=server)
    denied  = _require_edit(request, server)
    if denied:
        return denied
    profile.delete()
    messages.success(request, "Profil byl smazán.")
    return redirect(f"/servers/{slug}/profiles/")


@login_required
@require_POST
def profile_activate(request, slug, pk):
    server  = get_object_or_404(Server, slug=slug)
    profile = get_object_or_404(StartProfile, pk=pk, server=server)
    denied  = _require_edit(request, server)
    if denied:
        return denied
    profile.activate()
    messages.success(request, f"Profil '{profile.name}' je aktivni. Start prikaz byl aktualizovan.")
    return redirect(f"/servers/{slug}/profiles/")
