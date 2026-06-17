"""
config/urls.py  –  hlavní URL routing
"""
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from apps.setup.views import settings_view, runtime_status_view, test_email_view
from apps.servers.views import server_create, server_import, server_discover_api
from apps.filemanager.views import path_picker_api

urlpatterns = [
    path("admin/",    admin.site.urls),
    path("accounts/login/",           auth_views.LoginView.as_view(template_name="registration/login.html"),                    name="login"),
    path("accounts/logout/",          auth_views.LogoutView.as_view(),                                                             name="logout"),
    path("accounts/password-change/", auth_views.PasswordChangeView.as_view(template_name="registration/password_change.html",
                                      success_url="/accounts/password-change/done/"),                                               name="password_change"),
    path("accounts/password-change/done/", auth_views.PasswordChangeDoneView.as_view(template_name="registration/password_change_done.html"), name="password_change_done"),

    path("setup/",    include("apps.setup.urls")),
    path("settings/",            settings_view,    name="settings"),
    path("settings/test-email/", test_email_view,  name="test_email"),
    path("runtime/",  runtime_status_view, name="runtime_status"),
    path("users/",    include("apps.users.urls")),

    path("servers/new/", server_create, name="server_create"),
    path("servers/import/", server_import, name="server_import"),
    path("servers/discover/", server_discover_api, name="server_discover"),
    path("servers/path-picker/", path_picker_api, name="path_picker"),
    path("",          include("apps.dashboard.urls")),
    path("servers/",  include("apps.control.urls")),
    path("servers/",  include("apps.metrics.urls")),
    path("servers/",  include("apps.audit.urls")),
    path("servers/",  include("apps.alerts.urls")),
    path("servers/",  include("apps.filemanager.urls")),
    path("health/",   include("apps.common.urls")),
]

# Error handlers
handler400 = 'django.views.defaults.bad_request'
handler403 = 'django.views.defaults.permission_denied'
handler404 = 'django.views.defaults.page_not_found'
handler500 = 'django.views.defaults.server_error'
