# apps/control/urls.py
from django.urls import path
from . import views
from apps.users.views import server_access

app_name = "control"

urlpatterns = [
    path("<slug:slug>/actions/start/",      views.StartView.as_view(),       name="start"),
    path("<slug:slug>/actions/stop/",       views.StopView.as_view(),        name="stop"),
    path("<slug:slug>/actions/restart/",    views.RestartView.as_view(),     name="restart"),
    path("<slug:slug>/actions/force-stop/", views.ForceStopView.as_view(),   name="force_stop"),
    path("<slug:slug>/console/send/",       views.SendCommandView.as_view(), name="send_command"),
    path("<slug:slug>/access/",             server_access,                   name="server_access"),
]
