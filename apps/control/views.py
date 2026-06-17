"""
apps/control/views.py  (fáze 3)

Přidáno:
  - Rate limiting na všechny akce
  - Validace server konfigurace před startem
  - Lepší chybové zprávy
"""
import json
import logging
import os

from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.shortcuts import get_object_or_404

from apps.servers.models import Server
from apps.servers.validators import validate_server_config
from apps.users.permissions import can_control_server, can_view_server
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

    def require_control(self, request, server):
        if not can_control_server(request.user, server):
            return JsonResponse({"ok": False, "message": "Přístup odepřen."}, status=403)
        return None

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
        denied = self.require_control(request, server)
        if denied:
            return denied
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
        denied = self.require_control(request, server)
        if denied:
            return denied
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
        denied = self.require_control(request, server)
        if denied:
            return denied
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
        denied = self.require_control(request, server)
        if denied:
            return denied
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
        denied = self.require_control(request, server)
        if denied:
            return denied
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


class ConsoleStateView(ServerActionBase):
    def get(self, request, slug):
        server = self.get_server(slug)
        if not can_view_server(request.user, server):
            return JsonResponse({"ok": False, "message": "Přístup odepřen."}, status=403)

        try:
            from apps.control.service import backend
            get_state = getattr(backend, "get_console_state", None)
            if get_state is None:
                raise RuntimeError("console_state_unavailable")
            state = get_state(server) or {}
            return JsonResponse({
                "ok": True,
                "available": state.get("available", False),
                "can_write": state.get("can_write", False),
                "message": state.get("message", ""),
            })
        except Exception as exc:
            logger.exception("ConsoleStateView selhal pro %s", slug)
            return JsonResponse({"ok": False, "message": str(exc)}, status=500)


@method_decorator(require_POST, name="dispatch")
class BulkActionView(LoginRequiredMixin, View):
    """
    POST /servers/bulk/
    Body JSON: {"action": "start|stop|restart", "slugs": ["slug1", "slug2"]}
    Vrátí per-server výsledky.
    """
    raise_exception = True

    def post(self, request):
        if not request.user.is_staff:
            return JsonResponse({"ok": False, "message": "Vyžaduje admin oprávnění."}, status=403)

        try:
            body   = json.loads(request.body)
            action = body.get("action", "").strip()
            slugs  = body.get("slugs", [])
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({"ok": False, "message": "Neplatné tělo požadavku."}, status=400)

        ALLOWED = {"start", "stop", "restart"}
        if action not in ALLOWED:
            return JsonResponse({"ok": False, "message": f"Neznámá akce: {action}"}, status=400)

        if not slugs:
            return JsonResponse({"ok": False, "message": "Žádné servery nevybrány."}, status=400)

        results = []
        for slug in slugs[:20]:   # max 20 najednou
            try:
                server = Server.objects.get(slug=slug, is_active=True)
            except Server.DoesNotExist:
                results.append({"slug": slug, "ok": False, "message": "Server nenalezen."})
                continue

            if not can_control_server(request.user, server):
                results.append({"slug": slug, "ok": False, "message": "Přístup odepřen."})
                continue

            try:
                if action == "start":
                    r = start_server(server, user=request.user)
                elif action == "stop":
                    r = stop_server(server, user=request.user)
                else:
                    r = restart_server(server, user=request.user)
                results.append({"slug": slug, "ok": r.get("ok", True), "message": r.get("message", "")})
            except ServerLockError as e:
                results.append({"slug": slug, "ok": False, "message": str(e)})
            except Exception as e:
                logger.exception("Bulk action %s selhal pro %s", action, slug)
                results.append({"slug": slug, "ok": False, "message": "Interní chyba."})

        all_ok = all(r["ok"] for r in results)
        return JsonResponse({
            "ok":      all_ok,
            "results": results,
            "message": f"{sum(1 for r in results if r['ok'])}/{len(results)} serverů úspěšně.",
        })


class LogTailView(LoginRequiredMixin, View):
    """
    GET /servers/<slug>/console/logtail/?lines=200
    Vrátí posledních N řádků z log souboru serveru jako JSON.
    Funguje bez runtime workeru – čte soubor přímo.
    """
    raise_exception = True
    MAX_LINES = 500

    def get(self, request, slug):
        from apps.users.permissions import can_view_server
        server = get_object_or_404(Server, slug=slug, is_active=True)
        if not can_view_server(request.user, server):
            return JsonResponse({"ok": False, "lines": []}, status=403)

        n = min(int(request.GET.get("lines", 200)), self.MAX_LINES)
        log_path = server.log_file_path.strip() if server.log_file_path else ""
        if not log_path or os.path.isdir(log_path):
            # Fallback: panel_output.log ve working directory
            wd = server.working_directory.strip()
            if wd and not os.path.isdir(wd):
                return JsonResponse({"ok": True, "lines": [], "info": "working_directory neexistuje"})
            log_path = os.path.join(wd, "panel_output.log") if wd else ""
        if not log_path:
            return JsonResponse({"ok": True, "lines": [], "info": "log_file_path není nastaven"})

        try:
            import collections
            size = os.path.getsize(log_path)
            buf  = collections.deque(maxlen=n)
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                if size > 128 * 1024:
                    f.seek(max(0, size - 128 * 1024))
                    f.readline()  # přeskočí neúplný první řádek
                for line in f:
                    buf.append(line.rstrip("\r\n"))
            return JsonResponse({"ok": True, "lines": list(buf)})
        except FileNotFoundError:
            return JsonResponse({"ok": True, "lines": [], "info": "Log soubor zatím neexistuje"})
        except Exception as e:
            logger.warning("LogTailView %s: %s", slug, e)
            return JsonResponse({"ok": False, "lines": [], "info": str(e)})


class JavaDetectView(LoginRequiredMixin, View):
    """GET /api/java/detect/ — vrátí nalezené Java instalace."""
    raise_exception = True

    def get(self, request):
        if not request.user.is_staff:
            return JsonResponse({"ok": False}, status=403)
        from apps.servers.java_detect import detect_java
        installs = detect_java()
        return JsonResponse({
            "ok": True,
            "installs": [
                {"path": j.path, "version": j.version, "vendor": j.vendor,
                 "major": j.major, "label": j.label()}
                for j in installs
            ],
        })


class StatusView(LoginRequiredMixin, View):
    """
    GET /servers/<slug>/status/
    Vrátí aktuální stav serveru (zjistí přímo z procesu).
    """
    raise_exception = True

    def get(self, request, slug):
        from apps.users.permissions import can_view_server
        from apps.control.service import backend, _resolve_runtime_status
        server = get_object_or_404(Server, slug=slug, is_active=True)
        if not can_view_server(request.user, server):
            return JsonResponse({"ok": False}, status=403)

        try:
            info = backend.get_process_info(server)
            actual_status = _resolve_runtime_status(server, info)
            # Aktualizuj DB pokud se liší
            if server.status != actual_status:
                server.status = actual_status
                server.save(update_fields=["status"])
            return JsonResponse({
                "ok":     True,
                "status": actual_status,
                "pid":    info.pid,
                "cpu":    info.cpu_percent,
                "ram":    info.rss_bytes,
                "threads": info.thread_count,
            })
        except Exception as e:
            return JsonResponse({"ok": False, "status": server.status})
