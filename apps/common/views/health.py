"""
apps/common/views/health.py  (fáze 5)

Rozšířeno o backup status check.
GET /health/           – základní check
GET /health/?backups=1 – přidá backup status
"""
import time, logging, shutil
from django.http import JsonResponse
from django.views import View
from django.db import connection
import redis as redis_lib
from django.conf import settings

logger = logging.getLogger(__name__)

def _check_db():
    try:
        start = time.monotonic()
        with connection.cursor() as cur: cur.execute("SELECT 1")
        return {"ok": True, "latency_ms": round((time.monotonic()-start)*1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def _check_redis():
    try:
        url = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"][0]
        r   = redis_lib.from_url(url)
        start = time.monotonic(); r.ping()
        return {"ok": True, "latency_ms": round((time.monotonic()-start)*1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def _check_tmux():
    return {"ok": shutil.which("tmux") is not None}

def _check_backups():
    from apps.servers.models import Server
    from apps.servers.backup import check_backup_status
    results = {}
    for server in Server.objects.filter(is_active=True):
        if getattr(server, "backup_directory", ""):
            results[server.slug] = check_backup_status(server)
    return results

class HealthView(View):
    def get(self, request):
        checks = {"database": _check_db(), "redis": _check_redis(), "tmux": _check_tmux()}
        if request.GET.get("backups") == "1":
            checks["backups"] = _check_backups()
        all_ok = all(
            c["ok"] for c in checks.values()
            if isinstance(c, dict) and "ok" in c
        )
        return JsonResponse(
            {"status": "ok" if all_ok else "degraded", "checks": checks},
            status=200 if all_ok else 503,
        )
