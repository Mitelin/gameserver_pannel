from django.contrib import admin
from .models import UserProfile, ServerMembership


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display  = ("user", "panel_role", "created_at")
    list_filter   = ("panel_role",)
    search_fields = ("user__username",)


@admin.register(ServerMembership)
class ServerMembershipAdmin(admin.ModelAdmin):
    list_display  = ("user", "server", "role", "can_view", "can_control", "can_edit_config")
    list_filter   = ("role", "server")
    search_fields = ("user__username", "server__slug")
