"""
config/urls.py  –  hlavní URL routing
"""
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from apps.setup.views import settings_view

urlpatterns = [
    path("admin/",    admin.site.urls),
    path("accounts/login/",  auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),

    path("setup/",    include("apps.setup.urls")),
    path("settings/", settings_view, name="settings"),
    path("users/",    include("apps.users.urls")),

    path("",          include("apps.dashboard.urls")),
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
