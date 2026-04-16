# apps/control/urls.py
from django.urls import path
from . import views
from apps.users.views import server_access
from apps.servers import views as server_views

app_name = "control"

urlpatterns = [
    # Server akce
    path("<slug:slug>/actions/start/",      views.StartView.as_view(),       name="start"),
    path("<slug:slug>/actions/stop/",       views.StopView.as_view(),        name="stop"),
    path("<slug:slug>/actions/restart/",    views.RestartView.as_view(),     name="restart"),
    path("<slug:slug>/actions/force-stop/", views.ForceStopView.as_view(),   name="force_stop"),
    path("<slug:slug>/console/send/",       views.SendCommandView.as_view(), name="send_command"),
    path("<slug:slug>/access/",             server_access,                   name="server_access"),

    # Server config (fáze 4)
    path("<slug:slug>/edit/",              server_views.server_edit,       name="server_edit"),
    path("<slug:slug>/delete/",            server_views.server_delete,     name="server_delete"),
    path("<slug:slug>/properties/",        server_views.properties_editor, name="properties"),

    # Config history (fáze 5)
    path("<slug:slug>/history/",           server_views.server_history,    name="server_history"),

    # Start profily (Minecraft)
    path("<slug:slug>/profiles/",                      server_views.profile_list,     name="profile_list"),
    path("<slug:slug>/profiles/new/",                  server_views.profile_create,   name="profile_create"),
    path("<slug:slug>/profiles/<int:pk>/edit/",        server_views.profile_edit,     name="profile_edit"),
    path("<slug:slug>/profiles/<int:pk>/delete/",      server_views.profile_delete,   name="profile_delete"),
    path("<slug:slug>/profiles/<int:pk>/activate/",    server_views.profile_activate, name="profile_activate"),

    # Plánované restarty (fáze 7)
    path("<slug:slug>/schedule/",                      server_views.schedule_list,    name="schedule_list"),
    path("<slug:slug>/schedule/new/",                  server_views.schedule_create,  name="schedule_create"),
    path("<slug:slug>/schedule/<int:pk>/edit/",        server_views.schedule_edit,    name="schedule_edit"),
    path("<slug:slug>/schedule/<int:pk>/delete/",      server_views.schedule_delete,  name="schedule_delete"),
]
