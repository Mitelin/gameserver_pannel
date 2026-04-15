"""
apps/users/models.py

UserProfile  – globální role uživatele v panelu
ServerMembership – per-server přiřazení uživatele
"""
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class PanelRole(models.TextChoices):
    OWNER    = "owner",    "Owner"
    ADMIN    = "admin",    "Admin"
    OPERATOR = "operator", "Operator"
    VIEWER   = "viewer",   "Viewer"


# Výchozí boolean oprávnění pro každou roli
ROLE_DEFAULTS = {
    PanelRole.OWNER:    dict(can_view=True, can_control=True,  can_edit_config=True,  can_view_audit=True),
    PanelRole.ADMIN:    dict(can_view=True, can_control=True,  can_edit_config=True,  can_view_audit=True),
    PanelRole.OPERATOR: dict(can_view=True, can_control=True,  can_edit_config=False, can_view_audit=True),
    PanelRole.VIEWER:   dict(can_view=True, can_control=False, can_edit_config=False, can_view_audit=True),
}


class UserProfile(models.Model):
    """Globální role uživatele v panelu. Vždy existuje 1:1 k User."""
    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    panel_role = models.CharField(max_length=16, choices=PanelRole.choices, default=PanelRole.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Profile"

    @classmethod
    def get_for(cls, user) -> "UserProfile":
        """Bezpečný přístup – vytvoří profil pokud neexistuje."""
        profile, _ = cls.objects.get_or_create(user=user)
        return profile

    @property
    def is_owner(self) -> bool:
        return self.panel_role == PanelRole.OWNER

    @property
    def is_admin_or_above(self) -> bool:
        return self.panel_role in (PanelRole.OWNER, PanelRole.ADMIN)

    @property
    def is_operator_or_above(self) -> bool:
        return self.panel_role in (PanelRole.OWNER, PanelRole.ADMIN, PanelRole.OPERATOR)

    def __str__(self):
        return f"{self.user.username} ({self.get_panel_role_display()})"


class ServerMembership(models.Model):
    """Per-server přiřazení uživatele s konkrétními oprávněními."""
    user    = models.ForeignKey(User,             on_delete=models.CASCADE, related_name="server_memberships")
    server  = models.ForeignKey("servers.Server", on_delete=models.CASCADE, related_name="members")
    role    = models.CharField(max_length=16, choices=PanelRole.choices, default=PanelRole.VIEWER)

    can_view        = models.BooleanField(default=True)
    can_control     = models.BooleanField(default=False)
    can_edit_config = models.BooleanField(default=False)
    can_view_audit  = models.BooleanField(default=True)

    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "server")
        verbose_name = "Server Membership"
        ordering = ["server__name", "user__username"]

    def apply_role_defaults(self):
        """Nastaví boolean flags podle vybrané role."""
        defaults = ROLE_DEFAULTS.get(self.role, {})
        for field, value in defaults.items():
            setattr(self, field, value)

    def save(self, *args, **kwargs):
        # Pokud role změněna, aktualizuj flags (jen pokud nejsou explicitně nastaveny)
        if not self.pk:
            self.apply_role_defaults()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} → {self.server.slug} ({self.role})"
