"""
apps/control/views.py  (fáze 3)

Přidáno:
  - Rate limiting na všechny akce
  - Validace server konfigurace před startem
  - Lepší chybové zprávy
"""
import json
import logging

from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.shortcuts import get_object_or_404

from apps.servers.models import Server
from apps.servers.validators import validate_server_config
from apps.control.service import (
    start_server, stop_server, restart_server,
    force_stop_server, send_console_command,
    ServerLockError,
)
from apps.common.ratelimit import rate_limit, RateLimitExceeded

logger = logging.getLogger(__name__)


class ServerActionBase(LoginRequiredMixin, View):
    raise_exception = True

    def get_server(self, slug):
        return get_object_or_404(Server, slug=slug, is_active=True)

    def json_ok(self, result):
        return JsonResponse(result, status=200 if result.get("ok") else 400)

    def handle_rate_limit(self, exc):
        return JsonResponse({"ok": False, "message": str(exc)}, status=429)

    def handle_lock(self, exc):
        return JsonResponse({"ok": False, "message": str(exc)}, status=409)


@method_decorator(require_POST, name="dispatch")
class StartView(ServerActionBase):
    def post(self, request, slug):
        server = self.get_server(slug)
        try:
            rate_limit(request, key=f"start:{slug}", max_calls=3, period=60)
        except RateLimitExceeded as e:
            return self.handle_rate_limit(e)

        # Validace konfigurace před startem
        validation = validate_server_config(server)
        if not validation.ok:
            return JsonResponse({
                "ok":     False,
                "message": "Konfigurace serveru je neplatná.",
                "errors":  validation.errors,
            }, status=400)

        # Varování přidáme do response ale nezastavujeme
        try:
            result = start_server(server, user=request.user)
        except ServerLockError as e:
            return self.handle_lock(e)

        if validation.warnings:
            result["warnings"] = validation.warnings
        return self.json_ok(result)


@method_decorator(require_POST, name="dispatch")
class StopView(ServerActionBase):
    def post(self, request, slug):
        server = self.get_server(slug)
        try:
            rate_limit(request, key=f"stop:{slug}", max_calls=5, period=60)
        except RateLimitExceeded as e:
            return self.handle_rate_limit(e)
        try:
            result = stop_server(server, user=request.user)
        except ServerLockError as e:
            return self.handle_lock(e)
        return self.json_ok(result)


@method_decorator(require_POST, name="dispatch")
class RestartView(ServerActionBase):
    def post(self, request, slug):
        server = self.get_server(slug)
        try:
            rate_limit(request, key=f"restart:{slug}", max_calls=2, period=120)
        except RateLimitExceeded as e:
            return self.handle_rate_limit(e)

        validation = validate_server_config(server)
        if not validation.ok:
            return JsonResponse({
                "ok": False,
                "message": "Konfigurace serveru je neplatná.",
                "errors":  validation.errors,
            }, status=400)
        try:
            result = restart_server(server, user=request.user)
        except ServerLockError as e:
            return self.handle_lock(e)
        return self.json_ok(result)


@method_decorator(require_POST, name="dispatch")
class ForceStopView(ServerActionBase):
    def post(self, request, slug):
        if not request.user.is_staff:
            return JsonResponse(
                {"ok": False, "message": "Force-stop vyžaduje admin oprávnění."}, status=403
            )
        server = self.get_server(slug)
        try:
            rate_limit(request, key=f"force:{slug}", max_calls=2, period=60)
        except RateLimitExceeded as e:
            return self.handle_rate_limit(e)
        try:
            result = force_stop_server(server, user=request.user)
        except ServerLockError as e:
            return self.handle_lock(e)
        return self.json_ok(result)


@method_decorator(require_POST, name="dispatch")
class SendCommandView(ServerActionBase):
    def post(self, request, slug):
        server = self.get_server(slug)
        try:
            rate_limit(request, key=f"cmd:{slug}", max_calls=30, period=60)
        except RateLimitExceeded as e:
            return self.handle_rate_limit(e)
        try:
            body    = json.loads(request.body)
            command = body.get("command", "").strip()
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({"ok": False, "message": "Neplatné tělo požadavku."}, status=400)
        result = send_console_command(server, command, user=request.user)
        return self.json_ok(result)
