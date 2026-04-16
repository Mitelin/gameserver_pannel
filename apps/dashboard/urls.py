# apps/dashboard/urls.py
from django.urls import path
from . import views
from .players_view import player_history

app_name = "dashboard"

urlpatterns = [
    path("",                              views.server_list,      name="server_list"),
    path("servers/<slug:slug>/",          views.server_detail,    name="server_detail"),
    path("servers/<slug:slug>/status/",        views.server_status_api, name="server_status_api"),
    path("servers/<slug:slug>/backup-status/", views.backup_status_api, name="backup_status_api"),
    path("servers/<slug:slug>/log/download/",  views.log_download,      name="log_download"),
    path("servers/<slug:slug>/players/",       player_history,          name="player_history"),
    path("system-stats/",                      views.system_stats_api,  name="system_stats"),
]
