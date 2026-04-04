"""
apps/alerts/engine.py

Alert engine – vyhodnocuje pravidla a odesílá notifikace.

Volán z:
  - watchdog po změně stavu
  - metrics collector po každém sample (pro threshold alerts)
  - console tail po každém řádku (pro log_pattern alerts)
"""
import re
import logging
from datetime import timedelta

import requests
from django.utils import timezone

from apps.servers.models import Server
from apps.alerts.models import AlertRule, AlertFire, AlertConditionType

logger = logging.getLogger(__name__)


def check_status_alerts(server: Server, new_status: str):
    """Volej po každé změně stavu serveru."""
    rules = AlertRule.objects.filter(
        server=server,
        is_active=True,
        condition_type=AlertConditionType.STATUS_CHANGE,
    )
    for rule in rules:
        # status_values = "CRASHED,UNKNOWN" atd.
        trigger_statuses = [s.strip() for s in rule.status_values.split(",") if s.strip()]
        if new_status in trigger_statuses:
            _maybe_fire(rule, details=f"Status přešel na {new_status}", value=new_status)


def check_metric_alerts(server: Server, cpu_percent: float, ram_bytes: int):
    """Volej po každém metric sample."""
    rules = AlertRule.objects.filter(
        server=server,
        is_active=True,
        condition_type__in=[AlertConditionType.CPU_THRESHOLD, AlertConditionType.RAM_THRESHOLD],
    )
    for rule in rules:
        if rule.condition_type == AlertConditionType.CPU_THRESHOLD:
            if cpu_percent is not None and rule.threshold_value and cpu_percent >= rule.threshold_value:
                _maybe_fire(rule,
                            details=f"CPU {cpu_percent:.1f}% ≥ {rule.threshold_value}%",
                            value=f"{cpu_percent:.1f}%")

        elif rule.condition_type == AlertConditionType.RAM_THRESHOLD:
            ram_mb = (ram_bytes or 0) / 1048576
            if rule.threshold_value and ram_mb >= rule.threshold_value:
                _maybe_fire(rule,
                            details=f"RAM {ram_mb:.0f} MB ≥ {rule.threshold_value} MB",
                            value=f"{ram_mb:.0f} MB")


def check_no_players_alert(server: Server):
    """
    Volej periodicky (např. z watchdogu každou minutu).
    Zkontroluje, zda nejsou hráči déle než duration_minutes.
    """
    rules = AlertRule.objects.filter(
        server=server,
        is_active=True,
        condition_type=AlertConditionType.NO_PLAYERS,
    )
    try:
        state = server.process_state
    except Exception:
        return

    for rule in rules:
        if state.last_player_count and state.last_player_count > 0:
            continue  # hráči jsou přítomni
        if not rule.duration_minutes:
            continue

        # Kdy naposledy byl hráč? Použijeme last_log_line_at jako proxy
        # (lepší by byl PlayerSession model – fáze 5)
        cutoff = timezone.now() - timedelta(minutes=rule.duration_minutes)
        if state.last_log_line_at and state.last_log_line_at > cutoff:
            continue  # server je příliš čerstvý

        _maybe_fire(rule,
                    details=f"Žádný hráč déle než {rule.duration_minutes} minut",
                    value="0")


def check_log_pattern_alert(server: Server, line: str):
    """Volej pro každý nový řádek z log taileru."""
    rules = AlertRule.objects.filter(
        server=server,
        is_active=True,
        condition_type=AlertConditionType.LOG_PATTERN,
    )
    for rule in rules:
        if not rule.log_pattern:
            continue
        try:
            if re.search(rule.log_pattern, line):
                _maybe_fire(rule,
                            details=f"Pattern '{rule.log_pattern}' nalezen v logu",
                            value=line[:200])
        except re.error:
            logger.warning("Neplatný regex v AlertRule %s: %s", rule.id, rule.log_pattern)


# ─── Interní ─────────────────────────────────────────────────

def _maybe_fire(rule: AlertRule, details: str, value: str = ""):
    """Zkontroluje cooldown a odešle alert pokud je potřeba."""
    cooldown_cutoff = timezone.now() - timedelta(minutes=rule.cooldown_minutes)
    recent_fire = AlertFire.objects.filter(
        rule=rule,
        fired_at__gte=cooldown_cutoff,
    ).exists()

    if recent_fire:
        logger.debug("Alert '%s' v cooldown, přeskakuji.", rule.name)
        return

    _send_alert(rule, details, value)


def _send_alert(rule: AlertRule, details: str, value: str):
    """Odešle notifikaci na nakonfigurovaný kanál."""
    server = rule.server
    message = rule.message_template.format(
        server_name=server.name,
        status=server.status,
        value=value,
        condition=rule.get_condition_type_display(),
        details=details,
    )

    sent_ok = False
    if rule.channel == "webhook":
        sent_ok = _send_webhook(rule.webhook_url, message)

    AlertFire.objects.create(
        rule=rule,
        details=details,
        sent_ok=sent_ok,
    )

    if sent_ok:
        logger.info("Alert '%s' odeslán pro %s: %s", rule.name, server.slug, details)
    else:
        logger.error("Alert '%s' selhal pro %s", rule.name, server.slug)


def _send_webhook(url: str, message: str) -> bool:
    """Pošle Discord/Slack/Teams webhook."""
    try:
        resp = requests.post(
            url,
            json={"content": message},
            timeout=5,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Webhook selhal: %s", exc)
        return False
