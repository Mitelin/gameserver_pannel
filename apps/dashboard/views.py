"""
apps/dashboard/views.py  (fáze 4 + fáze 2 permissions)

Multi-server overview dashboard + server detail.
"""
import json
import logging
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.utils import timezone

from apps.servers.models import Server
from apps.servers.models import ServerStatus
from apps.console.buffer import CONSOLE_BUFFER_LIMIT, append_console_lines, get_console_lines
from apps.console.models import CommandHistory
from apps.audit.models import AuditEvent
from apps.users.permissions import accessible_servers, can_view_server, get_profile

logger = logging.getLogger(__name__)
INITIAL_CONSOLE_LINES = CONSOLE_BUFFER_LIMIT
UPDATE_STATE_PATH = Path(__file__).resolve().parents[2] / ".panel_update_state.json"
UPDATE_LOG_PATH = Path(__file__).resolve().parents[2] / ".panel_update.log"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _update_restart_plan() -> dict:
    env_cmd = (os.environ.get("GAMEPANEL_UPDATE_RESTART_CMD") or "").strip()
    if env_cmd:
        return {"mode": "custom", "command": env_cmd}

    web_service = (os.environ.get("GAMEPANEL_UPDATE_WEB_SERVICE") or "gameserver-panel-web.service").strip()
    worker_service = (os.environ.get("GAMEPANEL_UPDATE_WORKER_SERVICE") or "gameserver-panel-worker.service").strip()
    if shutil.which("systemctl"):
        return {
            "mode": "systemd",
            "web_service": web_service,
            "worker_service": worker_service,
        }

    return {"mode": "none"}


def _read_update_state() -> dict:
    if not UPDATE_STATE_PATH.exists():
        return {
            "status": "idle",
            "message": "Zadny update zatim nebyl spusten.",
            "started_at": None,
            "finished_at": None,
            "running": False,
            "pid": None,
        }
    try:
        data = json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "status": "error",
            "message": "Stav update nelze nacist.",
            "started_at": None,
            "finished_at": None,
            "running": False,
            "pid": None,
        }

    pid = data.get("pid")
    if data.get("running") and pid:
        try:
            import psutil
            if not psutil.pid_exists(int(pid)):
                data["running"] = False
                data["status"] = "unknown"
                data["message"] = "Update proces uz nebezi; zkontroluj log."
        except Exception:
            pass
    return data


def _write_update_state(payload: dict) -> None:
    UPDATE_STATE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _read_update_log_tail(lines: int = 60) -> list[str]:
    if not UPDATE_LOG_PATH.exists():
        return []
    try:
        with UPDATE_LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
        return [line.rstrip("\r\n") for line in content[-lines:]]
    except Exception:
        return []


def _run_git_command(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=_project_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _collect_update_info() -> dict:
    info = {
        "supported": platform.system().lower() == "linux",
        "git_available": shutil.which("git") is not None,
        "root": str(_project_root()),
        "branch": "",
        "local_head": "",
        "local_short": "",
        "remote_head": "",
        "remote_short": "",
        "ahead": 0,
        "behind": 0,
        "update_available": False,
        "check_error": "",
        "recent_commits": [],
        "restart_plan": _update_restart_plan(),
    }
    if not info["git_available"]:
        info["check_error"] = "Git neni dostupny v PATH."
        return info

    branch_res = _run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch_res.returncode != 0:
        info["check_error"] = (branch_res.stderr or branch_res.stdout or "Git branch nelze zjistit.").strip()
        return info
    branch = branch_res.stdout.strip()
    info["branch"] = branch

    local_head = _run_git_command(["rev-parse", "HEAD"])
    local_short = _run_git_command(["rev-parse", "--short", "HEAD"])
    info["local_head"] = local_head.stdout.strip() if local_head.returncode == 0 else ""
    info["local_short"] = local_short.stdout.strip() if local_short.returncode == 0 else ""

    fetch_res = _run_git_command(["fetch", "--quiet", "--prune", "origin"], timeout=60)
    if fetch_res.returncode != 0:
        info["check_error"] = (fetch_res.stderr or fetch_res.stdout or "Git fetch selhal.").strip()
        return info

    upstream_res = _run_git_command(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream_res.returncode != 0:
        info["check_error"] = (upstream_res.stderr or upstream_res.stdout or "Branch nema upstream.").strip()
        return info
    upstream = upstream_res.stdout.strip()

    remote_head = _run_git_command(["rev-parse", upstream])
    remote_short = _run_git_command(["rev-parse", "--short", upstream])
    info["remote_head"] = remote_head.stdout.strip() if remote_head.returncode == 0 else ""
    info["remote_short"] = remote_short.stdout.strip() if remote_short.returncode == 0 else ""

    compare_res = _run_git_command(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    if compare_res.returncode == 0:
        parts = compare_res.stdout.strip().split()
        if len(parts) == 2:
            info["ahead"] = int(parts[0])
            info["behind"] = int(parts[1])
            info["update_available"] = info["behind"] > 0

    log_res = _run_git_command(["log", "--oneline", "--decorate", f"HEAD..{upstream}", "-n", "5"])
    if log_res.returncode == 0:
        info["recent_commits"] = [line for line in log_res.stdout.splitlines() if line.strip()]
    return info


def _json_shell_write(path: Path, json_payload: str) -> str:
    return "printf '%s\\n' {payload} > {path}".format(
        payload=shlex.quote(json_payload),
        path=shlex.quote(str(path)),
    )


def _build_update_shell_command() -> str:
    root = shlex.quote(str(_project_root()))
    python_executable = shlex.quote(sys.executable)
    log_path = shlex.quote(str(UPDATE_LOG_PATH))
    restart_plan = _update_restart_plan()

    restart_cmd = f"echo '[update] restart skipped: no restart command configured' >> {log_path}"
    if restart_plan["mode"] == "custom":
        restart_cmd = f"bash -lc {shlex.quote(restart_plan['command'])}"
    elif restart_plan["mode"] == "systemd":
        worker = shlex.quote(restart_plan["worker_service"])
        web = shlex.quote(restart_plan["web_service"])
        restart_cmd = (
            f"(systemctl restart {worker} || sudo -n systemctl restart {worker} || true); "
            f"(systemctl restart {web} || sudo -n systemctl restart {web} || true)"
        )

    noop_state = json.dumps({
        "status": "noop",
        "message": "Neni k dispozici zadna nova verze.",
        "running": False,
        "finished_at": None,
        "pid": None,
    }, ensure_ascii=True)
    complete_state = json.dumps({
        "status": "completed",
        "message": "Update byl dokoncen; restart panelu byl vyzadan.",
        "running": False,
        "finished_at": None,
        "pid": None,
    }, ensure_ascii=True)

    return f"""
set -eu
STEP='initialization'
mark_failed() {{
  { _json_shell_write(UPDATE_STATE_PATH, json.dumps({"status": "error", "message": "UPDATE selhal.", "running": False, "finished_at": None, "pid": None}, ensure_ascii=True)) }
  echo "[update] failed at step: $STEP" >> {log_path}
}}
trap mark_failed ERR
cd {root}
printf '' > {log_path}
echo '[update] started at '$(date -Iseconds) >> {log_path}
STEP='detect branch'
BRANCH=$(git rev-parse --abbrev-ref HEAD)
STEP='fetch origin'
git fetch --prune origin >> {log_path} 2>&1
STEP='compare commits'
BEHIND=$(git rev-list --right-only --count HEAD...origin/$BRANCH)
if [ "$BEHIND" -eq 0 ]; then
  { _json_shell_write(UPDATE_STATE_PATH, noop_state) }
  echo '[update] no changes detected' >> {log_path}
  exit 0
fi
STEP='pull changes'
git pull --ff-only origin "$BRANCH" >> {log_path} 2>&1
STEP='install requirements'
{python_executable} -m pip install -r requirements.txt >> {log_path} 2>&1
STEP='apply migrations'
{python_executable} manage.py migrate --noinput >> {log_path} 2>&1
STEP='collect static'
{python_executable} manage.py collectstatic --noinput >> {log_path} 2>&1
{ _json_shell_write(UPDATE_STATE_PATH, complete_state) }
echo '[update] update completed, requesting restart' >> {log_path}
STEP='restart services'
{restart_cmd} >> {log_path} 2>&1 || true
""".strip()


def _start_update_process() -> dict:
    state = _read_update_state()
    if state.get("running"):
        return {"ok": False, "message": "Update uz prave bezi."}

    shell_command = _build_update_shell_command()
    UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["bash", "-lc", shell_command],
        cwd=_project_root(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _write_update_state({
        "status": "running",
        "message": "Update byl spusten na pozadi.",
        "started_at": timezone.now().isoformat(),
        "finished_at": None,
        "running": True,
        "pid": proc.pid,
    })
    return {"ok": True, "message": "Update byl spusten na pozadi."}


def _host_disk_usage(path_hint: str | None = None):
    import psutil

    candidates = []
    if path_hint:
        candidates.append(path_hint)
        try:
            path_obj = Path(path_hint)
            if path_obj.anchor:
                candidates.append(path_obj.anchor)
        except Exception:
            pass

    candidates.extend(["/", str(Path.cwd().anchor or "/")])

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return psutil.disk_usage(candidate)
        except Exception:
            continue
    return None


@login_required
def server_list(request):
    """Multi-server overview – hlavní dashboard."""
    from apps.servers.models import ServerStatus

    base_qs = Server.objects.filter(is_active=True).select_related("process_state")
    servers  = accessible_servers(request.user, base_qs).order_by("name")

    total   = servers.count()
    online  = servers.filter(status=ServerStatus.ONLINE).count()
    crashed = servers.filter(status=ServerStatus.CRASHED).count()
    offline = servers.filter(status=ServerStatus.OFFLINE).count()

    profile = get_profile(request.user)

    ctx = {
        "servers": servers,
        "stats": {
            "total":   total,
            "online":  online,
            "crashed": crashed,
            "offline": offline,
        },
        "profile": profile,
    }
    return render(request, "dashboard/server_list.html", ctx)


@login_required
def update_overview(request):
    if not request.user.is_staff:
        raise PermissionDenied

    if request.method == "POST":
        info = _collect_update_info()
        if not info["supported"]:
            messages.error(request, "UPDATE workflow je urceny jen pro Linux nasazeni.")
            return redirect("dashboard:update_overview")
        if not info["git_available"]:
            messages.error(request, "Git neni dostupny v PATH.")
            return redirect("dashboard:update_overview")

        result = _start_update_process()
        if result["ok"]:
            messages.success(request, result["message"])
        else:
            messages.error(request, result["message"])
        return redirect("dashboard:update_overview")

    ctx = {
        "update_info": _collect_update_info(),
        "update_state": _read_update_state(),
        "update_log_tail": _read_update_log_tail(),
    }
    return render(request, "dashboard/update.html", ctx)


@login_required
def server_detail(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied

    try:
        process_state = server.process_state
    except Exception:
        process_state = None

    initial_lines = []
    if server.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
        initial_lines = get_console_lines(server.id, INITIAL_CONSOLE_LINES)
    if server.status in (ServerStatus.ONLINE, ServerStatus.STARTING) and not initial_lines:
        try:
            from apps.control.service import backend
            get_recent_console_lines = getattr(backend, "get_recent_console_lines", None)
            if get_recent_console_lines is not None:
                recent_lines = get_recent_console_lines(server, INITIAL_CONSOLE_LINES)
                if recent_lines:
                    append_console_lines(
                        server.id,
                        [("stdout", line) for line in recent_lines],
                        source="backend_replay",
                        is_live=False,
                    )
                    initial_lines = get_console_lines(server.id, INITIAL_CONSOLE_LINES)
        except Exception:
            logger.debug("Nepodařilo se načíst replay konzole z backendu", exc_info=True)

    recent_commands = CommandHistory.objects.filter(server=server)[:20]
    recent_events   = AuditEvent.objects.filter(server=server)[:20]

    # Alert rules pro tento server
    from apps.alerts.models import AlertRule
    alert_rules = AlertRule.objects.filter(server=server, is_active=True)

    try:
        active_profile = server.start_profiles.filter(is_active=True).first()
    except Exception:
        active_profile = None

    ctx = {
        "server":         server,
        "process_state":  process_state,
        "initial_lines":  initial_lines,
        "recent_commands": recent_commands,
        "recent_events":  recent_events,
        "alert_rules":    alert_rules,
        "ws_url":         f"/ws/servers/{server.slug}/",
        "rcon_enabled":   server.rcon_enabled,
        "active_profile": active_profile,
    }
    return render(request, "dashboard/server_detail.html", ctx)


def _build_backup_summary(backups: list[dict]) -> dict:
    return {
        "total_count": len(backups),
        "auto_count": sum(1 for item in backups if not item.get("is_user")),
        "user_count": sum(1 for item in backups if item.get("is_user")),
        "total_size_bytes": sum(item.get("size_bytes", 0) for item in backups),
        "kept_intraday": sum(1 for item in backups if item.get("retention_bucket") == "intraday"),
        "kept_daily": sum(1 for item in backups if item.get("retention_bucket") == "daily"),
        "kept_weekly": sum(1 for item in backups if item.get("retention_bucket") == "weekly"),
        "kept_monthly": sum(1 for item in backups if item.get("retention_bucket") == "monthly"),
    }


@login_required
def backup_overview(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied

    if request.method == "POST":
        if not request.user.is_staff:
            raise PermissionDenied

        import threading
        from apps.servers.backup_engine import create_backup

        def _run():
            create_backup(server, is_user=True)

        threading.Thread(target=_run, daemon=True).start()
        messages.success(request, "USER záloha spuštěna na pozadí. Automatická rotace ji nebude mazat.")
        return redirect("dashboard:backup_overview", slug=server.slug)

    from apps.servers.backup import check_backup_status
    from apps.servers.backup_engine import list_backups

    backups = list_backups(server)
    ctx = {
        "server": server,
        "backups": backups,
        "backup_status": check_backup_status(server),
        "summary": _build_backup_summary(backups),
        "latest_backup": backups[0] if backups else None,
    }
    return render(request, "dashboard/server_backups.html", ctx)


@login_required
def backup_status_api(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied
    from apps.servers.backup import check_backup_status
    return JsonResponse(check_backup_status(server))


@login_required
def backup_create_api(request, slug):
    """POST – spustí zálohu serveru v pozadí."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "Pouze POST."}, status=405)

    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not request.user.is_staff:
        raise PermissionDenied

    import threading
    from apps.servers.backup_engine import create_backup
    is_user = request.POST.get("kind") == "user"

    def _run():
        create_backup(server, is_user=is_user)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    kind_label = "USER " if is_user else ""
    return JsonResponse({"ok": True, "message": f"{kind_label}záloha spuštěna na pozadí."})


@login_required
def backup_list_api(request, slug):
    """Vrátí seznam záloh serveru."""
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied
    from apps.servers.backup_engine import list_backups
    return JsonResponse({"backups": list_backups(server)})


@login_required
def log_download(request, slug):
    """Stáhne posledních 5000 řádků log souboru serveru."""
    import os
    from django.http import HttpResponse

    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied

    log_path = server.log_file_path
    if not log_path or not os.path.exists(log_path):
        return HttpResponse("Log soubor nenalezen.", status=404, content_type="text/plain; charset=utf-8")

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-5000:])
        response = HttpResponse(content, content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{server.slug}-latest.log"'
        return response
    except Exception as exc:
        return HttpResponse(f"Chyba čtení logu: {exc}", status=500, content_type="text/plain; charset=utf-8")


@login_required
def system_stats_api(request):
    """Vrátí aktuální využití CPU/RAM/disku hostitele."""
    import psutil
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    disk = _host_disk_usage()
    data = {
        "ok":               True,
        "cpu_percent":      cpu,
        "ram_percent":      ram.percent,
        "ram_used_bytes":   ram.used,
        "ram_total_bytes":  ram.total,
    }
    if disk:
        data.update({
            "disk_percent":     disk.percent,
            "disk_used_bytes":  disk.used,
            "disk_total_bytes": disk.total,
        })
    return JsonResponse(data)


@login_required
def server_status_api(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        raise PermissionDenied
    warnings = []
    try:
        from apps.control.service import backend, _resolve_runtime_status
        info = backend.get_process_info(server)
        actual_status = _resolve_runtime_status(server, info)
        if server.status != actual_status:
            server.status = actual_status
            server.save(update_fields=["status"])
        players = 0
        try:
            players = server.process_state.last_player_count
        except Exception as exc:
            warnings.append(f"player_count_unavailable:{exc.__class__.__name__}")
        disk_used = disk_free = disk_total = None
        try:
            d = _host_disk_usage(server.working_directory)
            if d is None:
                raise RuntimeError("disk_usage_unavailable")
            disk_used, disk_free, disk_total = d.used, d.free, d.total
        except Exception as exc:
            warnings.append(f"disk_usage_unavailable:{exc.__class__.__name__}")

        return JsonResponse({
            "ok":          True,
            "status":      actual_status,
            "pid":         info.pid,
            "cpu_percent": info.cpu_percent,
            "ram_bytes":   info.rss_bytes,
            "players":     players,
            "threads":     info.thread_count,
            "disk_used":   disk_used,
            "disk_free":   disk_free,
            "disk_total":  disk_total,
            "warnings":    warnings,
        })
    except Exception as exc:
        logger.exception("server_status_api selhalo pro %s", slug)
        return JsonResponse({
            "ok": False,
            "status": server.status,
            "error": str(exc),
            "warnings": warnings,
        }, status=500)
