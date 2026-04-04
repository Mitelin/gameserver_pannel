# apps/audit/urls.py
from django.urls import path
from . import views

app_name = "audit"

urlpatterns = [
    path("<slug:slug>/audit/",     views.audit_log,     name="log"),
    path("<slug:slug>/audit/api/", views.audit_log_api, name="api"),
]
