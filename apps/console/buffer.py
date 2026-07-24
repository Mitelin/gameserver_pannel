from __future__ import annotations

from threading import RLock

from django.core.cache import cache
from django.utils import timezone


CONSOLE_BUFFER_LIMIT = 1000
CONSOLE_BUFFER_TTL = 60 * 60 * 12
CONSOLE_ACTIVITY_WRITE_INTERVAL_SECONDS = 5

_buffer_lock = RLock()


def _buffer_key(server_id: int) -> str:
    return f"console.buffer.{server_id}"


def _activity_key(server_id: int) -> str:
    return f"console.activity.{server_id}"


def _store_console_lines(
    server_id: int,
    batch: list[tuple[str, str]],
    source: str,
    *,
    is_live: bool,
    replace: bool,
) -> list[dict]:
    if not batch:
        return []

    now = timezone.now().isoformat()
    with _buffer_lock:
        items = [] if replace else list(cache.get(_buffer_key(server_id), []))
        for stream_type, line in batch:
            items.append({
                "timestamp": now,
                "line": line,
                "stream_type": stream_type,
                "source": source,
                "is_live": is_live,
            })
        if len(items) > CONSOLE_BUFFER_LIMIT:
            items = items[-CONSOLE_BUFFER_LIMIT:]
        cache.set(_buffer_key(server_id), items, CONSOLE_BUFFER_TTL)
        return items[-len(batch):]


def append_console_lines(
    server_id: int,
    batch: list[tuple[str, str]],
    source: str,
    *,
    is_live: bool = True,
) -> list[dict]:
    return _store_console_lines(server_id, batch, source, is_live=is_live, replace=False)


def replace_console_lines(
    server_id: int,
    batch: list[tuple[str, str]],
    source: str,
    *,
    is_live: bool = False,
) -> list[dict]:
    return _store_console_lines(server_id, batch, source, is_live=is_live, replace=True)


def get_console_lines(server_id: int, limit: int | None = None, *, live_only: bool = False) -> list[dict]:
    items = list(cache.get(_buffer_key(server_id), []))
    if live_only:
        items = [item for item in items if item.get("is_live", True)]
    if limit is None or limit >= len(items):
        return items
    return items[-limit:]


def clear_console_lines(server_id: int) -> None:
    with _buffer_lock:
        cache.delete(_buffer_key(server_id))


def touch_console_activity(server, now=None) -> None:
    now = now or timezone.now()
    cache.set(_activity_key(server.id), now.isoformat(), CONSOLE_BUFFER_TTL)

    try:
        state = server.process_state
    except Exception:
        return

    last = state.last_log_line_at
    if last and (now - last).total_seconds() < CONSOLE_ACTIVITY_WRITE_INTERVAL_SECONDS:
        return

    state.last_log_line_at = now
    state.save(update_fields=["last_log_line_at"])