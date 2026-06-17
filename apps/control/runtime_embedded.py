from __future__ import annotations

import os
import sys
import threading
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_start_lock = threading.Lock()
_started = False
_stop_event = None
_threads = []


def _runserver_uses_autoreload() -> bool:
    return "--noreload" not in sys.argv


def _should_start_embedded_runtime() -> bool:
    if not getattr(settings, "USE_INMEMORY_CHANNEL_LAYER", False):
        return False
    if "runserver" not in sys.argv:
        return False
    if settings.DEBUG and _runserver_uses_autoreload() and os.environ.get("RUN_MAIN") != "true":
        return False
    return True


def maybe_start_embedded_runtime() -> bool:
    global _started, _stop_event, _threads

    if not _should_start_embedded_runtime():
        return False

    with _start_lock:
        if _started:
            return False

        from apps.control.management.commands.run_runtime_worker import start_runtime_threads

        _stop_event, _threads = start_runtime_threads()
        _started = True
        if settings.DEBUG and not _runserver_uses_autoreload():
            logger.info("Embedded runtime běží v runserver --noreload režimu, aby se neztratily live subprocess handly.")
        logger.info("Embedded runtime worker spuštěn v runserver procesu (%d threadů).", len(_threads))
        return True