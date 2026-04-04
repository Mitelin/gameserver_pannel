# apps/alerts/urls.py
from django.urls import path
from . import views

app_name = "alerts"

urlpatterns = [
    path("<slug:slug>/rcon/",  views.RconCommandView.as_view(), name="rcon"),
    path("<slug:slug>/alerts/",views.AlertRuleListView.as_view(), name="list"),
]
