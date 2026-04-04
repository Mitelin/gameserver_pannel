"""
config/asgi.py

ASGI entry point pro Django Channels.
HTTP requestu jdou do Django, WebSocket do consumers.
"""
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import re_path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_asgi_app = get_asgi_application()

from apps.console.consumers import ServerConsumer  # import po setup settings

websocket_urlpatterns = [
    re_path(r"ws/servers/(?P<slug>[\w-]+)/$", ServerConsumer.as_asgi()),
]

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})
