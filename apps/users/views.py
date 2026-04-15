"""
apps/users/views.py

/users/              – seznam uživatelů (Owner only)
/users/<id>/         – edit uživatele – role + server memberships (Owner only)
/users/new/          – vytvoření nového uživatele (Owner only)
/servers/<slug>/access/ – správa přístupu k serveru (Admin+)
"""
import logging

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.core.exceptions import PermissionDenied

from apps.servers.models import Server
from .models import UserProfile, ServerMembership, PanelRole, ROLE_DEFAULTS
from .permissions import (
    require_owner, require_admin_or_above,
    get_profile, is_admin_or_above,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# ── /users/ – seznam uživatelů ────────────────────────────────────────────────

@require_owner
def user_list(request):
    users = (
        User.objects
        .prefetch_related("profile", "server_memberships")
        .order_by("username")
    )
    # Doplň profil těm, kteří ho nemají
    rows = []
    for u in users:
        try:
            profile = u.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile.get_for(u)
        rows.append({
            "user":          u,
            "profile":       profile,
            "server_count":  u.server_memberships.count(),
        })
    return render(request, "users/user_list.html", {"rows": rows, "roles": PanelRole})


# ── /users/new/ – nový uživatel ───────────────────────────────────────────────

@require_owner
def user_create(request):
    if request.method == "POST":
        username   = request.POST.get("username", "").strip()
        email      = request.POST.get("email", "").strip()
        password   = request.POST.get("password", "")
        panel_role = request.POST.get("panel_role", PanelRole.VIEWER)

        errors = []
        if not username:
            errors.append("Uživatelské jméno je povinné.")
        elif User.objects.filter(username=username).exists():
            errors.append("Uživatel s tímto jménem již existuje.")
        if len(password) < 8:
            errors.append("Heslo musí mít alespoň 8 znaků.")
        if panel_role not in PanelRole.values:
            errors.append("Neplatná role.")

        if errors:
            return render(request, "users/user_create.html", {
                "errors": errors, "roles": PanelRole,
                "post": request.POST,
            })

        user = User.objects.create_user(username=username, email=email, password=password)
        profile = UserProfile.get_for(user)
        profile.panel_role = panel_role
        profile.save()

        messages.success(request, f"Uživatel {username} byl vytvořen.")
        return redirect(f"/users/{user.pk}/")

    return render(request, "users/user_create.html", {"roles": PanelRole})


# ── /users/<id>/ – detail + edit uživatele ───────────────────────────────────

@require_owner
def user_edit(request, user_id):
    target  = get_object_or_404(User, pk=user_id)
    profile = UserProfile.get_for(target)
    memberships = (
        ServerMembership.objects
        .filter(user=target)
        .select_related("server")
        .order_by("server__name")
    )
    all_servers = Server.objects.filter(is_active=True).order_by("name")
    member_server_ids = set(memberships.values_list("server_id", flat=True))

    if request.method == "POST":
        action = request.POST.get("action")

        # ── Změna globální role ──────────────────────────────────────────
        if action == "set_role":
            new_role = request.POST.get("panel_role", "")
            if new_role not in PanelRole.values:
                messages.error(request, "Neplatná role.")
            elif target == request.user and new_role != PanelRole.OWNER:
                messages.error(request, "Nemůžeš si snížit vlastní roli Owner.")
            else:
                profile.panel_role = new_role
                profile.save()
                messages.success(request, f"Role uživatele {target.username} změněna na {new_role}.")
            return redirect(f"/users/{user_id}/")

        # ── Přidání server membership ────────────────────────────────────
        if action == "add_membership":
            server_id = request.POST.get("server_id")
            role      = request.POST.get("role", PanelRole.VIEWER)
            server = get_object_or_404(Server, pk=server_id, is_active=True)
            if role not in PanelRole.values:
                messages.error(request, "Neplatná role.")
            elif server.id in member_server_ids:
                messages.error(request, f"Uživatel již má přístup k {server.name}.")
            else:
                m = ServerMembership(user=target, server=server, role=role)
                m.save()  # apply_role_defaults se volá v save()
                messages.success(request, f"Přidán přístup k {server.name} ({role}).")
            return redirect(f"/users/{user_id}/")

        # ── Odebrání server membership ───────────────────────────────────
        if action == "remove_membership":
            membership_id = request.POST.get("membership_id")
            m = get_object_or_404(ServerMembership, pk=membership_id, user=target)
            server_name = m.server.name
            m.delete()
            messages.success(request, f"Přístup k {server_name} odebrán.")
            return redirect(f"/users/{user_id}/")

        # ── Aktivace / deaktivace účtu ───────────────────────────────────
        if action == "toggle_active":
            if target == request.user:
                messages.error(request, "Nemůžeš deaktivovat vlastní účet.")
            else:
                target.is_active = not target.is_active
                target.save()
                state = "aktivován" if target.is_active else "deaktivován"
                messages.success(request, f"Účet {target.username} byl {state}.")
            return redirect(f"/users/{user_id}/")

    ctx = {
        "target":          target,
        "profile":         profile,
        "memberships":     memberships,
        "all_servers":     all_servers,
        "member_server_ids": member_server_ids,
        "roles":           PanelRole,
        "is_self":         target == request.user,
    }
    return render(request, "users/user_edit.html", ctx)


# ── /servers/<slug>/access/ – per-server access ──────────────────────────────

@require_admin_or_above
def server_access(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)

    # Operator může vidět access jen vlastního serveru
    if not is_admin_or_above(request.user):
        raise PermissionDenied

    memberships = (
        ServerMembership.objects
        .filter(server=server)
        .select_related("user")
        .order_by("user__username")
    )
    member_ids = set(memberships.values_list("user_id", flat=True))
    all_users  = User.objects.filter(is_active=True).exclude(pk__in=member_ids).order_by("username")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            user_id = request.POST.get("user_id")
            role    = request.POST.get("role", PanelRole.VIEWER)
            user    = get_object_or_404(User, pk=user_id, is_active=True)
            if role not in PanelRole.values:
                messages.error(request, "Neplatná role.")
            else:
                m = ServerMembership(user=user, server=server, role=role)
                m.save()
                messages.success(request, f"{user.username} přidán ({role}).")
            return redirect(f"/servers/{slug}/access/")

        if action == "remove":
            membership_id = request.POST.get("membership_id")
            m = get_object_or_404(ServerMembership, pk=membership_id, server=server)
            username = m.user.username
            m.delete()
            messages.success(request, f"{username} odebrán.")
            return redirect(f"/servers/{slug}/access/")

        if action == "change_role":
            membership_id = request.POST.get("membership_id")
            new_role      = request.POST.get("role", PanelRole.VIEWER)
            m = get_object_or_404(ServerMembership, pk=membership_id, server=server)
            if new_role not in PanelRole.values:
                messages.error(request, "Neplatná role.")
            else:
                m.role = new_role
                m.apply_role_defaults()
                m.save()
                messages.success(request, f"Role {m.user.username} změněna na {new_role}.")
            return redirect(f"/servers/{slug}/access/")

    ctx = {
        "server":      server,
        "memberships": memberships,
        "all_users":   all_users,
        "roles":       PanelRole,
    }
    return render(request, "users/server_access.html", ctx)
