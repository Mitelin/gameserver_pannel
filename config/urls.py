"""
config/urls.py  –  hlavní URL routing
"""
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from apps.setup.views import settings_view, runtime_status_view
from apps.servers.views import server_create

urlpatterns = [
    path("admin/",    admin.site.urls),
    path("accounts/login/",  auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),

    path("setup/",    include("apps.setup.urls")),
    path("settings/", settings_view,       name="settings"),
    path("runtime/",  runtime_status_view, name="runtime_status"),
    path("users/",    include("apps.users.urls")),

    path("",          include("apps.dashboard.urls")),
    path("servers/new/", server_create, name="server_create"),
    path("servers/",  include("apps.control.urls")),
    path("servers/",  include("apps.metrics.urls")),
    path("servers/",  include("apps.audit.urls")),
    path("servers/",  include("apps.alerts.urls")),
    path("health/",   include("apps.common.urls")),
]

# Error handlers
handler400 = 'django.views.defaults.bad_request'
handler403 = 'django.views.defaults.permission_denied'
handler404 = 'django.views.defaults.page_not_found'
handler500 = 'django.views.defaults.server_error'
