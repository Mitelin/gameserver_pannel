"""
apps/setup/views.py

Setup wizard (/setup/<step>/) + Settings stránka (/settings/).
"""
import subprocess
import secrets
import logging
from pathlib import Path

from django.shortcuts import render, redirect
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils import timezone as tz
from django.contrib import messages

from .forms import AdminAccountForm, WizardStep3Form, SystemSettingsForm, FirstServerForm
from .models import BootstrapState, SystemSettings

logger = logging.getLogger(__name__)
User = get_user_model()

STEP_LABELS = {
    1: "Vítejte",
    2: "Správce",
    3: "Nastavení",
    4: "Kontrola",
    5: "První server",
    6: "Dokončení",
}
TOTAL_STEPS = len(STEP_LABELS)
SESSION_KEY = "setup_wizard_data"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wizard_data(request) -> dict:
    return request.session.get(SESSION_KEY, {})


def _save_step(request, key: str, data):
    all_data = _wizard_data(request)
    all_data[key] = data
    request.session[SESSION_KEY] = all_data
    request.session.modified = True


def _step_ctx(step: int) -> dict:
    return {
        "current_step": step,
        "total_steps":  TOTAL_STEPS,
        "step_labels":  STEP_LABELS,
        "prev_step":    step - 1 if step > 1 else None,
    }


# ── Index / dispatcher ────────────────────────────────────────────────────────

def setup_index(request):
    if BootstrapState.is_done():
        return redirect("/")
    return redirect("/setup/1/")


def setup_step_view(request, step):
    if BootstrapState.is_done():
        return redirect("/")
    if step < 1 or step > TOTAL_STEPS:
        return redirect("/setup/1/")

    handlers = {1: _step1, 2: _step2, 3: _step3, 4: _step4, 5: _step5, 6: _step6}
    return handlers[step](request)


# ── Step 1 – Vítejte ──────────────────────────────────────────────────────────

def _step1(request):
    if request.method == "POST":
        return redirect("/setup/2/")
    return render(request, "setup/step1.html", _step_ctx(1))


# ── Step 2 – Správce ──────────────────────────────────────────────────────────

def _step2(request):
    saved = _wizard_data(request).get("step2", {})
    if request.method == "POST":
        form = AdminAccountForm(request.POST)
        if form.is_valid():
            _save_step(request, "step2", {
                "username": form.cleaned_data["username"],
                "email":    form.cleaned_data.get("email", ""),
                "password": form.cleaned_data["password"],
            })
            return redirect("/setup/3/")
    else:
        form = AdminAccountForm(initial={"username": saved.get("username", ""),
                                         "email":    saved.get("email", "")})
    return render(request, "setup/step2.html", {**_step_ctx(2), "form": form})


# ── Step 3 – Nastavení ────────────────────────────────────────────────────────

def _step3(request):
    saved = _wizard_data(request).get("step3", {})
    if request.method == "POST":
        form = WizardStep3Form(request.POST)
        if form.is_valid():
            _save_step(request, "step3", form.cleaned_data)
            return redirect("/setup/4/")
    else:
        form = WizardStep3Form(initial=saved or None)
    return render(request, "setup/step3.html", {**_step_ctx(3), "form": form})


# ── Step 4 – Runtime check ────────────────────────────────────────────────────

def _check_database() -> dict:
    try:
        from django.db import connection
        connection.ensure_connection()
        engine = connection.settings_dict.get("ENGINE", "").split(".")[-1]
        return {"ok": True,  "label": "Databáze", "detail": f"OK ({engine})", "optional": False}
    except Exception as exc:
        return {"ok": False, "label": "Databáze", "detail": str(exc), "optional": False}


def _check_redis() -> dict:
    try:
        from django.conf import settings as djsettings
        import redis as redis_lib
        r = redis_lib.from_url(getattr(djsettings, "REDIS_URL", "redis://127.0.0.1:6379/0"))
        r.ping()
        return {"ok": True,  "label": "Redis", "detail": "Připojení OK", "optional": True}
    except Exception as exc:
        return {"ok": False, "label": "Redis",
                "detail": f"Nedostupný – {exc.__class__.__name__}", "optional": True}


def _check_tmux() -> dict:
    try:
        result = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            return {"ok": True,  "label": "tmux", "detail": result.stdout.strip(), "optional": True}
        return {"ok": False, "label": "tmux", "detail": "Chyba volání", "optional": True}
    except FileNotFoundError:
        return {"ok": False, "label": "tmux",
                "detail": "Není nainstalován. Panel může použít subprocess backend, ale tmux je vhodný pro linuxový provoz.", "optional": True}
    except Exception as exc:
        return {"ok": False, "label": "tmux", "detail": str(exc), "optional": True}


def _check_secret_key() -> dict:
    from django.conf import settings as djsettings
    key = djsettings.SECRET_KEY
    insecure_defaults = {"change-me-in-production", "change-me"}
    is_insecure = any(key.startswith(d) for d in insecure_defaults) or len(key) < 32
    if is_insecure:
        new_key = secrets.token_urlsafe(50)
        return {
            "ok": False,
            "label": "SECRET_KEY",
            "detail": f"Nesecure výchozí hodnota! Doporučená: {new_key}",
            "optional": True,
        }
    return {"ok": True, "label": "SECRET_KEY", "detail": "OK", "optional": True}


def _step4(request):
    if request.method == "POST":
        return redirect("/setup/5/")
    checks = [_check_database(), _check_redis(), _check_tmux(), _check_secret_key()]
    all_critical_ok = all(c["ok"] for c in checks if not c["optional"])
    return render(request, "setup/step4.html", {
        **_step_ctx(4),
        "checks": checks,
        "all_critical_ok": all_critical_ok,
    })


# ── Step 5 – První server ─────────────────────────────────────────────────────

def _step5(request):
    saved = _wizard_data(request).get("step5", {})
    if request.method == "POST":
        if "skip" in request.POST:
            _save_step(request, "step5", None)
            return redirect("/setup/6/")
        form = FirstServerForm(request.POST)
        if form.is_valid():
            _save_step(request, "step5", form.cleaned_data)
            return redirect("/setup/6/")
    else:
        form = FirstServerForm(initial=saved or None)
    return render(request, "setup/step5.html", {**_step_ctx(5), "form": form})


# ── Step 6 – Dokončení ────────────────────────────────────────────────────────

def _step6(request):
    data = _wizard_data(request)

    if request.method == "POST":
        step2 = data.get("step2")
        step3 = data.get("step3")

        if not step2 or not step3:
            messages.error(request, "Chybí data z předchozích kroků – začni znova od kroku 1.")
            return redirect("/setup/1/")

        try:
            # 1. Vytvoř admin uživatele
            user = User.objects.create_superuser(
                username=step2["username"],
                email=step2.get("email", ""),
                password=step2["password"],
            )

            # 1b. Přiřaď Owner roli
            from apps.users.models import UserProfile, PanelRole
            profile = UserProfile.get_for(user)
            profile.panel_role = PanelRole.OWNER
            profile.save()

            # 2. SystemSettings
            ss = SystemSettings.get()
            for field, value in step3.items():
                if hasattr(ss, field):
                    setattr(ss, field, value)
            ss.save()

            # 3. Volitelný první server
            step5 = data.get("step5")
            if step5:
                from apps.servers.models import Server
                Server.objects.create(
                    name              = step5["name"],
                    slug              = step5["slug"],
                    game_type         = step5["game_type"],
                    working_directory = step5["working_directory"],
                    start_command     = step5["start_command"],
                    stop_command      = step5.get("stop_command", ""),
                    tmux_session_name = step5["tmux_session_name"],
                    log_file_path     = step5["log_file_path"],
                )

            # 4. Označ bootstrap jako hotový
            bs = BootstrapState.get()
            bs.is_completed = True
            bs.completed_at = tz.now()
            bs.completed_by = user
            bs.save()

            # 5. Přihlas uživatele
            login(request, user)

            # 6. Vymaž session data wizardu
            request.session.pop(SESSION_KEY, None)

            messages.success(request, f"Panel byl úspěšně nastaven. Vítej, {user.username}!")
            return redirect("/")

        except Exception:
            logger.exception("Chyba při dokončení bootstrapu")
            messages.error(request, "Při nastavení došlo k chybě. Zkontroluj logy.")
            return redirect("/setup/6/")

    ctx = {
        **_step_ctx(6),
        "step2":    data.get("step2"),
        "step3":    data.get("step3"),
        "step5":    data.get("step5"),
        "has_data": bool(data.get("step2") and data.get("step3")),
    }
    return render(request, "setup/step6.html", ctx)


# ── Runtime status stránka (/runtime/) ───────────────────────────────────────

_WORKER_LOOPS = [
    {"key": "runtime.heartbeat.console",   "name": "Console tailer",      "warn_after": 5,  "dead_after": 30},
    {"key": "runtime.heartbeat.metrics",   "name": "Metrics collector",   "warn_after": 30, "dead_after": 60},
    {"key": "runtime.heartbeat.watchdog",  "name": "Server watchdog",     "warn_after": 15, "dead_after": 45},
    {"key": "runtime.heartbeat.scheduler", "name": "Restart scheduler",   "warn_after": 60, "dead_after": 120},
]


def _is_staff(user):
    return user.is_active and (user.is_staff or user.is_superuser)


@login_required
@user_passes_test(_is_staff)
def runtime_status_view(request):
    import datetime
    from django.core.cache import cache
    from django.utils.timezone import now as utcnow

    loops = []
    all_ok = True

    for cfg in _WORKER_LOOPS:
        raw = cache.get(cfg["key"])
        if raw:
            last_tick = datetime.datetime.fromisoformat(raw)
            if last_tick.tzinfo is None:
                last_tick = last_tick.replace(tzinfo=datetime.timezone.utc)
            age = (utcnow() - last_tick).total_seconds()
            if age <= cfg["warn_after"]:
                state = "ok"
            elif age <= cfg["dead_after"]:
                state = "warn"
                all_ok = False
            else:
                state = "dead"
                all_ok = False
        else:
            last_tick = None
            age       = None
            state     = "stopped"
            all_ok    = False

        loops.append({
            "name":      cfg["name"],
            "last_tick": last_tick,
            "age":       round(age, 1) if age is not None else None,
            "state":     state,
        })

    return render(request, "runtime/status.html", {"loops": loops, "all_ok": all_ok})


# ── Settings stránka (/settings/) ────────────────────────────────────────────

@login_required
@user_passes_test(_is_staff)
def settings_view(request):
    obj = SystemSettings.get()

    if request.method == "POST":
        form = SystemSettingsForm(request.POST)
        if form.is_valid():
            for field, value in form.cleaned_data.items():
                if hasattr(obj, field):
                    setattr(obj, field, value)
            obj.save()
            messages.success(request, "Nastavení bylo uloženo.")
            return redirect("/settings/")
    else:
        initial = {f: getattr(obj, f) for f in SystemSettingsForm.base_fields if hasattr(obj, f)}
        form = SystemSettingsForm(initial=initial)

    return render(request, "settings/index.html", {"form": form, "obj": obj})


def test_email_view(request):
    """POST /settings/test-email/ – pošle testovací email."""
    from django.contrib.auth.decorators import login_required
    from django.http import JsonResponse
    from django.views.decorators.http import require_POST

    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"ok": False, "message": "Přístup odepřen."}, status=403)

    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "Pouze POST."}, status=405)

    to_address = request.POST.get("email", "").strip()
    if not to_address:
        return JsonResponse({"ok": False, "message": "Zadej emailovou adresu."})

    from apps.alerts.engine import send_test_email
    ok, msg = send_test_email(to_address)
    return JsonResponse({"ok": ok, "message": msg})
