"""
apps/servers/views.py  (fáze 4 + fáze 5 + fáze 9)

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
  - schedule_list      GET       /servers/<slug>/schedule/
  - schedule_create    GET/POST  /servers/<slug>/schedule/new/
  - schedule_edit      GET/POST  /servers/<slug>/schedule/<pk>/edit/
  - schedule_delete    POST      /servers/<slug>/schedule/<pk>/delete/
  - server_export      GET       /servers/<slug>/export/  (JSON download)
  - server_import      GET/POST  /servers/import/         (JSON upload)
"""
import json
import logging
from pathlib import Path

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse, HttpResponse
from django.views.decorators.http import require_POST

from apps.users.permissions import can_edit_server_config, is_admin_or_above
from .models import Server, StartProfile, GameType, ScheduledRestart
from .forms import ServerForm, StartProfileForm, ScheduledRestartForm

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


# ── Plánované restarty ────────────────────────────────────────────────────────

@login_required
def schedule_list(request, slug):
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied

    from croniter import croniter
    from datetime import datetime

    schedules = list(server.scheduled_restarts.all())
    # Přidej info o příším spuštění
    enriched = []
    for s in schedules:
        try:
            nxt = croniter(s.cron_expression, datetime.now()).get_next(datetime)
        except Exception:
            nxt = None
        enriched.append({"obj": s, "next": nxt})

    return render(request, "servers/schedule_list.html", {
        "server":    server,
        "schedules": enriched,
    })


@login_required
def schedule_create(request, slug):
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied

    if request.method == "POST":
        form = ScheduledRestartForm(request.POST)
        if form.is_valid():
            s = form.save(commit=False)
            s.server = server
            s.save()
            messages.success(request, f"Plánovaný restart '{s.get_label()}' byl vytvořen.")
            return redirect(f"/servers/{slug}/schedule/")
    else:
        form = ScheduledRestartForm()

    return render(request, "servers/schedule_edit.html", {
        "form":      form,
        "server":    server,
        "is_create": True,
    })


@login_required
def schedule_edit(request, slug, pk):
    server   = get_object_or_404(Server, slug=slug)
    schedule = get_object_or_404(ScheduledRestart, pk=pk, server=server)
    denied   = _require_edit(request, server)
    if denied:
        return denied

    if request.method == "POST":
        form = ScheduledRestartForm(request.POST, instance=schedule)
        if form.is_valid():
            form.save()
            messages.success(request, "Plán byl uložen.")
            return redirect(f"/servers/{slug}/schedule/")
    else:
        form = ScheduledRestartForm(instance=schedule)

    return render(request, "servers/schedule_edit.html", {
        "form":      form,
        "server":    server,
        "schedule":  schedule,
        "is_create": False,
    })


@login_required
@require_POST
def schedule_delete(request, slug, pk):
    server   = get_object_or_404(Server, slug=slug)
    schedule = get_object_or_404(ScheduledRestart, pk=pk, server=server)
    denied   = _require_edit(request, server)
    if denied:
        return denied
    label = schedule.get_label()
    schedule.delete()
    messages.success(request, f"Plán '{label}' byl smazán.")
    return redirect(f"/servers/{slug}/schedule/")


# ── Export / Import ───────────────────────────────────────────────────────────

_EXPORT_FIELDS = [
    "name", "slug", "game_type", "is_active",
    "working_directory", "start_command", "stop_command",
    "tmux_session_name", "log_file_path", "pid_file_path", "host_label",
    "rcon_enabled", "rcon_host", "rcon_port",
    "expected_startup_seconds", "expected_shutdown_seconds",
    "webhook_url", "webhook_on_crash", "webhook_on_start", "webhook_on_stop",
    "backup_directory", "backup_max_age_hours",
]


@login_required
def server_export(request, slug):
    """Stáhne konfiguraci serveru jako JSON soubor."""
    server = get_object_or_404(Server, slug=slug)
    denied = _require_edit(request, server)
    if denied:
        return denied

    data = {field: getattr(server, field) for field in _EXPORT_FIELDS}
    data["_export_version"] = 1

    # Exportuj i start profily
    data["start_profiles"] = [
        {
            "name":        p.name,
            "is_active":   p.is_active,
            "jar_file":    p.jar_file,
            "heap_min_mb": p.heap_min_mb,
            "heap_max_mb": p.heap_max_mb,
            "jvm_flags":   p.jvm_flags,
            "extra_args":  p.extra_args,
        }
        for p in server.start_profiles.all()
    ]

    content  = json.dumps(data, indent=2, ensure_ascii=False)
    response = HttpResponse(content, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{server.slug}-config.json"'
    return response


@login_required
def server_import(request):
    """Import konfigurace serveru z JSON souboru."""
    if not is_admin_or_above(request.user):
        return HttpResponseForbidden("Nedostatečná oprávnění.")

    errors = []

    if request.method == "POST":
        uploaded = request.FILES.get("config_file")
        if not uploaded:
            errors.append("Soubor nebyl nahrán.")
        else:
            try:
                raw  = uploaded.read().decode("utf-8")
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                errors.append(f"Neplatný JSON soubor: {e}")
                data = None

            if data and not errors:
                # Zkontroluj povinná pole
                required = ["name", "slug", "game_type", "working_directory",
                            "start_command", "tmux_session_name", "log_file_path"]
                for field in required:
                    if not data.get(field):
                        errors.append(f"Chybí povinné pole: {field}")

                # Kontrola duplicitního slugu
                new_slug = data.get("slug", "")
                if not errors and Server.objects.filter(slug=new_slug).exists():
                    errors.append(f"Server se slugem '{new_slug}' již existuje. Přejmenuj slug v JSON souboru.")

                if not errors:
                    # Vytvoř server
                    server_data = {
                        field: data[field]
                        for field in _EXPORT_FIELDS
                        if field in data
                    }
                    server_data.pop("_export_version", None)
                    server = Server.objects.create(**server_data)

                    # Import start profilů
                    profiles = data.get("start_profiles", [])
                    from .models import StartProfile
                    for p in profiles:
                        StartProfile.objects.create(
                            server=server,
                            name=p.get("name", "Importovaný profil"),
                            is_active=p.get("is_active", False),
                            jar_file=p.get("jar_file", "server.jar"),
                            heap_min_mb=p.get("heap_min_mb", 512),
                            heap_max_mb=p.get("heap_max_mb", 2048),
                            jvm_flags=p.get("jvm_flags", ""),
                            extra_args=p.get("extra_args", ""),
                        )

                    messages.success(request, f"Server '{server.name}' byl importován.")
                    return redirect(f"/servers/{server.slug}/edit/")

    return render(request, "servers/server_import.html", {"errors": errors})
