"""
apps/setup/middleware.py

Přesměruje každý požadavek na /setup/ pokud nebyl dokončen bootstrap wizard.
Vynechá statiku, admin, accounts a samotné /setup/ cesty.
"""
from django.shortcuts import redirect

# Cesty přístupné bez dokončeného bootstrapu
_EXEMPT_PREFIXES = (
    "/setup/",
    "/static/",
    "/admin/",
    "/accounts/",
    "/health/",
    "/__debug__/",
)


class BootstrapMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._done = False      # in-process cache; nastaví se True po potvrzení z DB

    def __call__(self, request):
        if not self._is_exempt(request.path):
            if not self._is_bootstrap_done():
                return redirect("/setup/")
        return self.get_response(request)

    # ------------------------------------------------------------------ #

    def _is_exempt(self, path: str) -> bool:
        return any(path.startswith(p) for p in _EXEMPT_PREFIXES)

    def _is_bootstrap_done(self) -> bool:
        # Pokud máme pozitivní cache, ověříme ji jednou za 60 požadavků
        # (ochrana před situací kdy se DB resetuje při vývoji)
        if self._done:
            self._cache_hits = getattr(self, "_cache_hits", 0) + 1
            if self._cache_hits < 60:
                return True
            self._cache_hits = 0   # revalidate

        try:
            from apps.setup.models import BootstrapState
            result = BootstrapState.is_done()
            if result:
                self._done = True
            else:
                self._done = False  # cache invalidace (např. při resetu DB)
            return result
        except Exception:
            # DB ještě není ready – neblokujeme
            return True
