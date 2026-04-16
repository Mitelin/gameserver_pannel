"""
apps/alerts/views.py

RCON endpoint, přehled a správa alert rules.
"""
import json
import logging

from django.http import JsonResponse, HttpResponseForbidden
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from apps.servers.models import Server
from apps.alerts.models import AlertRule
from apps.users.permissions import can_edit_server_config, can_view_server

logger = logging.getLogger(__name__)


@method_decorator(require_POST, name="dispatch")
class RconCommandView(LoginRequiredMixin, View):
    """
    POST /servers/<slug>/rcon/
    Body: {"command": "list"}
    Pošle příkaz přes RCON a vrátí odpověď serveru.
    """
    raise_exception = True

    def post(self, request, slug):
        server = get_object_or_404(Server, slug=slug, is_active=True)

        if not server.rcon_enabled:
            return JsonResponse({"ok": False, "message": "RCON není na tomto serveru povoleno."}, status=400)

        try:
            body    = json.loads(request.body)
            command = body.get("command", "").strip()
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({"ok": False, "message": "Neplatné tělo požadavku."}, status=400)

        if not command:
            return JsonResponse({"ok": False, "message": "Prázdný příkaz."}, status=400)

        from apps.control.backends.rcon import RconClient, RconError, RconAuthError
        try:
            with RconClient(server.rcon_host, server.rcon_port, server.rcon_password) as rcon:
                response = rcon.command(command)
            logger.info("RCON [%s] %s → %s", server.slug, command, response[:100])
            return JsonResponse({"ok": True, "response": response})
        except RconAuthError as exc:
            return JsonResponse({"ok": False, "message": f"RCON autentizace selhala: {exc}"}, status=403)
        except RconError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=503)


class AlertRuleListView(LoginRequiredMixin, View):
    """
    GET /servers/<slug>/alerts/  → seznam alert rules pro server
    """
    raise_exception = True

    def get(self, request, slug):
        server = get_object_or_404(Server, slug=slug, is_active=True)
        rules  = AlertRule.objects.filter(server=server).prefetch_related("fires")

        data = []
        for rule in rules:
            last_fire = rule.fires.first()
            data.append({
                "id":              str(rule.id),
                "name":            rule.name,
                "is_active":       rule.is_active,
                "condition_type":  rule.condition_type,
                "threshold_value": rule.threshold_value,
                "cooldown_minutes":rule.cooldown_minutes,
                "last_fired":      last_fire.fired_at.isoformat() if last_fire else None,
                "last_sent_ok":    last_fire.sent_ok if last_fire else None,
            })

        return JsonResponse({"rules": data})


# ── Alert rules UI ────────────────────────────────────────────────────────────

@login_required
def alert_rule_list(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_view_server(request.user, server):
        return HttpResponseForbidden()

    rules = AlertRule.objects.filter(server=server).prefetch_related("fires")
    rules_with_last_fire = [
        (rule, rule.fires.first())
        for rule in rules
    ]
    return render(request, "alerts/rule_list.html", {
        "server":             server,
        "rules_with_last_fire": rules_with_last_fire,
        "can_edit":           can_edit_server_config(request.user, server),
    })


@login_required
def alert_rule_create(request, slug):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    if not can_edit_server_config(request.user, server):
        return HttpResponseForbidden("Nemáš oprávnění upravovat alerty tohoto serveru.")

    from .forms import AlertRuleForm
    if request.method == "POST":
        form = AlertRuleForm(request.POST)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.server = server
            rule.save()
            messages.success(request, f"Alert '{rule.name}' byl vytvořen.")
            return redirect(f"/servers/{slug}/alerts/")
    else:
        form = AlertRuleForm()

    return render(request, "alerts/rule_edit.html", {
        "form":      form,
        "server":    server,
        "rule":      None,
        "is_create": True,
    })


@login_required
def alert_rule_edit(request, slug, pk):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    rule   = get_object_or_404(AlertRule, pk=pk, server=server)
    if not can_edit_server_config(request.user, server):
        return HttpResponseForbidden()

    from .forms import AlertRuleForm
    if request.method == "POST":
        form = AlertRuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.save()
            messages.success(request, "Alert byl uložen.")
            return redirect(f"/servers/{slug}/alerts/")
    else:
        form = AlertRuleForm(instance=rule)

    return render(request, "alerts/rule_edit.html", {
        "form":      form,
        "server":    server,
        "rule":      rule,
        "is_create": False,
    })


@login_required
@require_POST
def alert_rule_delete(request, slug, pk):
    server = get_object_or_404(Server, slug=slug, is_active=True)
    rule   = get_object_or_404(AlertRule, pk=pk, server=server)
    if not can_edit_server_config(request.user, server):
        return HttpResponseForbidden()
    rule.delete()
    messages.success(request, "Alert byl smazán.")
    return redirect(f"/servers/{slug}/alerts/")
