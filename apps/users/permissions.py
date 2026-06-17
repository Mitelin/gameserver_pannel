"""
apps/users/permissions.py

Pomocné funkce a dekorátory pro kontrolu oprávnění.

Logika:
  - Owner / Admin   → přístup ke všem serverům
  - Operator / Viewer → jen servery kde mají ServerMembership
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404

from .models import PanelRole, UserProfile, ServerMembership


# ── Profil ────────────────────────────────────────────────────────────────────

def get_profile(user) -> UserProfile:
    return UserProfile.get_for(user)


def _has_global_admin(user) -> bool:
    if not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return get_profile(user).is_admin_or_above


# ── Globální role checks ──────────────────────────────────────────────────────

def is_owner(user) -> bool:
    if not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return get_profile(user).panel_role == PanelRole.OWNER


def is_admin_or_above(user) -> bool:
    if not user.is_authenticated:
        return False
    return _has_global_admin(user)


def is_operator_or_above(user) -> bool:
    if not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return get_profile(user).panel_role in (PanelRole.OWNER, PanelRole.ADMIN, PanelRole.OPERATOR)


# ── Server-level checks ───────────────────────────────────────────────────────

def _membership(user, server):
    return ServerMembership.objects.filter(user=user, server=server).first()


def can_view_server(user, server) -> bool:
    if not user.is_authenticated:
        return False
    if _has_global_admin(user):
        return True
    m = _membership(user, server)
    return m is not None and m.can_view


def can_control_server(user, server) -> bool:
    if not user.is_authenticated:
        return False
    if _has_global_admin(user):
        return True
    m = _membership(user, server)
    return m is not None and m.can_control


def can_edit_server_config(user, server) -> bool:
    if not user.is_authenticated:
        return False
    if _has_global_admin(user):
        return True
    m = _membership(user, server)
    return m is not None and m.can_edit_config


def can_view_server_audit(user, server) -> bool:
    if not user.is_authenticated:
        return False
    if _has_global_admin(user):
        return True
    m = _membership(user, server)
    return m is not None and m.can_view_audit


def accessible_servers(user, queryset=None):
    """Vrátí queryset serverů přístupných danému uživateli."""
    from apps.servers.models import Server
    if queryset is None:
        queryset = Server.objects.filter(is_active=True)
    if not user.is_authenticated:
        return queryset.none()
    if _has_global_admin(user):
        return queryset
    ids = (
        ServerMembership.objects
        .filter(user=user, can_view=True)
        .values_list("server_id", flat=True)
    )
    return queryset.filter(id__in=ids)


# ── Dekorátory ────────────────────────────────────────────────────────────────

def require_owner(view_func):
    """Jen Owner."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not is_owner(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def require_admin_or_above(view_func):
    """Owner nebo Admin."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not is_admin_or_above(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def require_server_view(view_func):
    """Ověří can_view_server – server se lookupuje přes kwarg 'slug'."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        from apps.servers.models import Server
        slug = kwargs.get("slug")
        server = get_object_or_404(Server, slug=slug, is_active=True)
        if not can_view_server(request.user, server):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def require_server_control(view_func):
    """Ověří can_control_server."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        from apps.servers.models import Server
        slug = kwargs.get("slug")
        server = get_object_or_404(Server, slug=slug, is_active=True)
        if not can_control_server(request.user, server):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper
