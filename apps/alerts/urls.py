# apps/alerts/urls.py
from django.urls import path
from . import views

app_name = "alerts"

urlpatterns = [
    path("<slug:slug>/rcon/",                       views.RconCommandView.as_view(),  name="rcon"),
    path("<slug:slug>/alerts/",                     views.AlertRuleListView.as_view(), name="list_api"),

    # HTML UI (fáze 5)
    path("<slug:slug>/alerts/manage/",              views.alert_rule_list,   name="rule_list"),
    path("<slug:slug>/alerts/new/",                 views.alert_rule_create, name="rule_create"),
    path("<slug:slug>/alerts/<uuid:pk>/edit/",      views.alert_rule_edit,   name="rule_edit"),
    path("<slug:slug>/alerts/<uuid:pk>/delete/",    views.alert_rule_delete, name="rule_delete"),
]
