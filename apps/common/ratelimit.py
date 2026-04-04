"""
apps/common/ratelimit.py

Jednoduchý rate limiter přes Django cache (Redis).

Použití ve views:
    from apps.common.ratelimit import rate_limit, RateLimitExceeded

    class StartView(ServerActionBase):
        def post(self, request, slug):
            try:
                rate_limit(request, key=f"start:{slug}", max_calls=3, period=60)
            except RateLimitExceeded as e:
                return JsonResponse({"ok": False, "message": str(e)}, status=429)
            ...
"""
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    pass


def rate_limit(request, key: str, max_calls: int = 5, period: int = 60):
    """
    Zvyšuje počítadlo volání pro daný klíč.
    Vyvolá RateLimitExceeded pokud je překročen limit.

    key    – unikátní klíč (typicky "action:server_slug")
    max_calls – maximální počet volání
    period – časové okno v sekundách
    """
    user_id  = request.user.id if request.user.is_authenticated else "anon"
    cache_key = f"rl:{user_id}:{key}"

    current = cache.get(cache_key, 0)
    if current >= max_calls:
        logger.warning("Rate limit překročen: user=%s key=%s", user_id, key)
        raise RateLimitExceeded(
            f"Příliš mnoho požadavků. Zkus to za {period} sekund."
        )

    # Inkrementace – add vytvoří klíč s TTL, incr zvýší existující
    if current == 0:
        cache.set(cache_key, 1, timeout=period)
    else:
        cache.incr(cache_key)
