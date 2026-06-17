from django.apps import AppConfig


class ControlConfig(AppConfig):
    name = "apps.control"

    def ready(self):
        try:
            from apps.control.runtime_embedded import maybe_start_embedded_runtime
            maybe_start_embedded_runtime()
        except Exception:
            # Embedded runtime je jen dev convenience; nesmí shodit app init.
            pass