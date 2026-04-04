"""
apps/control/restart_handler.py

Pomocná funkce volaná z watchdogu:
Pokud server přešel do OFFLINE a v cache je pending_restart flag,
automaticky ho znovu spustí.
"""
import logging
from django.core.cache import cache
from apps.servers.models import Server, ServerStatus

logger = logging.getLogger(__name__)


def maybe_auto_restart(server: Server):
    """
    Zavolej poté, co server přejde do OFFLINE.
    Pokud čeká restart, spustí server znovu.
    """
    flag_key = f"pending_restart:{server.id}"
    if not cache.get(flag_key):
        return

    cache.delete(flag_key)
    logger.info("Auto-restart: spouštím %s po shutdown.", server.slug)

    from apps.control.service import start_server
    result = start_server(server, user=None)
    if result["ok"]:
        logger.info("Auto-restart %s: %s", server.slug, result["message"])
    else:
        logger.error("Auto-restart %s selhal: %s", server.slug, result["message"])
