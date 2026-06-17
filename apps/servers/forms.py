"""
apps/servers/forms.py

Formuláře pro správu serverů a start profilů.
"""
import os
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace

from django import forms
from django.utils.text import slugify

from .discovery import read_properties_file
from .models import Server, StartProfile, GameType, ScheduledRestart


def _coalesce(value, fallback):
    if value in (None, ""):
        return fallback
    return value


def _normalize_path(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return os.path.normcase(os.path.normpath(value))


def _default_log_path(game_type: str, working_directory: str) -> str:
    if not working_directory:
        return ""
    root = Path(working_directory)
    if game_type == GameType.MINECRAFT_JAVA:
        return str(root / "logs" / "latest.log")
    if game_type == GameType.MINECRAFT_BEDROCK:
        return str(root / "bedrock_server.log")
    if game_type == GameType.TERRARIA:
        return str(root / "server.log")
    if game_type == GameType.FACTORIO:
        return str(root / "factorio-current.log")
    return str(root / "panel_output.log")


def _derive_slug(name: str, instance: Server | None = None) -> str:
    base = slugify(name or "") or "server"
    candidate = base
    suffix = 2
    qs = Server.objects.all()
    if instance and instance.pk:
        qs = qs.exclude(pk=instance.pk)
    while qs.filter(slug=candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


class ServerForm(forms.ModelForm):
    """Vytvoření / editace serveru."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in [
            "slug", "stop_command", "tmux_session_name", "log_file_path",
            "expected_startup_seconds", "expected_shutdown_seconds",
            "rcon_host", "rcon_port", "rcon_password",
        ]:
            self.fields[name].required = False
        self.fields["rcon_enabled"].required = False

    def clean(self):
        cleaned = super().clean()

        name = (cleaned.get("name") or getattr(self.instance, "name", "")).strip()
        game_type = cleaned.get("game_type") or getattr(self.instance, "game_type", GameType.OTHER)
        working_directory = (cleaned.get("working_directory") or getattr(self.instance, "working_directory", "")).strip()

        if working_directory:
            normalized_workdir = _normalize_path(working_directory)
            active_servers = Server.objects.filter(is_active=True).exclude(pk=self.instance.pk).only(
                "slug", "name", "working_directory"
            )
            for existing in active_servers:
                if _normalize_path(existing.working_directory) == normalized_workdir:
                    raise forms.ValidationError(
                        f"Pracovní adresář už používá aktivní server '{existing.name}' ({existing.slug})."
                    )

        if not (cleaned.get("slug") or "").strip():
            cleaned["slug"] = _derive_slug(name, self.instance)

        if not (cleaned.get("tmux_session_name") or "").strip():
            cleaned["tmux_session_name"] = cleaned["slug"].replace("-", "_")[:64]

        if not (cleaned.get("stop_command") or "").strip():
            cleaned["stop_command"] = "exit" if game_type == GameType.TERRARIA else "stop"

        if not (cleaned.get("log_file_path") or "").strip():
            cleaned["log_file_path"] = _default_log_path(game_type, working_directory)

        if not (cleaned.get("backup_directory") or "").strip():
            cleaned["backup_directory"] = working_directory

        cleaned["expected_startup_seconds"] = int(_coalesce(
            cleaned.get("expected_startup_seconds"),
            getattr(self.instance, "expected_startup_seconds", Server._meta.get_field("expected_startup_seconds").default),
        ))
        cleaned["expected_shutdown_seconds"] = int(_coalesce(
            cleaned.get("expected_shutdown_seconds"),
            getattr(self.instance, "expected_shutdown_seconds", Server._meta.get_field("expected_shutdown_seconds").default),
        ))

        props = {}
        if game_type == GameType.MINECRAFT_JAVA and working_directory:
            props = read_properties_file(Path(working_directory) / "server.properties")

        current_enabled = getattr(self.instance, "rcon_enabled", False)
        current_host = getattr(self.instance, "rcon_host", "")
        current_port = getattr(self.instance, "rcon_port", None)
        current_password = getattr(self.instance, "rcon_password", "")

        props_enabled = props.get("enable-rcon", "false").lower() == "true" or bool(props.get("rcon.password"))
        props_host = "127.0.0.1"
        props_port = int(props.get("rcon.port", "25575") or "25575") if props else None
        props_password = props.get("rcon.password", "")

        if game_type == GameType.MINECRAFT_JAVA:
            cleaned["rcon_enabled"] = bool(cleaned.get("rcon_enabled") or current_enabled or props_enabled)
            cleaned["rcon_host"] = (cleaned.get("rcon_host") or current_host or props_host).strip()
            cleaned["rcon_port"] = int(_coalesce(cleaned.get("rcon_port"), current_port or props_port or 25575))
            cleaned["rcon_password"] = (cleaned.get("rcon_password") or current_password or props_password).strip()
        else:
            cleaned["rcon_enabled"] = bool(cleaned.get("rcon_enabled") or current_enabled)
            cleaned["rcon_host"] = (cleaned.get("rcon_host") or current_host).strip()
            cleaned["rcon_port"] = _coalesce(cleaned.get("rcon_port"), current_port)
            cleaned["rcon_password"] = (cleaned.get("rcon_password") or current_password).strip()

        return cleaned

    class Meta:
        model  = Server
        fields = [
            "name", "slug", "game_type", "is_active",
            "working_directory", "start_command", "stop_command", "tmux_session_name",
            "log_file_path",
            "expected_startup_seconds", "expected_shutdown_seconds",
            "rcon_enabled", "rcon_host", "rcon_port", "rcon_password",
            "webhook_url", "webhook_on_crash", "webhook_on_start", "webhook_on_stop",
            "backup_directory", "backup_max_age_hours",
        ]
        widgets = {
            "name":              forms.TextInput(attrs={"class": "field-input"}),
            "slug":              forms.TextInput(attrs={"class": "field-input"}),
            "game_type":         forms.Select(attrs={"class": "field-input"}),
            "working_directory": forms.TextInput(attrs={"class": "field-input", "placeholder": "/srv/mc"}),
            "start_command":     forms.TextInput(attrs={"class": "field-input", "placeholder": "java -jar server.jar --nogui"}),
            "stop_command":      forms.TextInput(attrs={"class": "field-input", "placeholder": "stop"}),
            "tmux_session_name": forms.TextInput(attrs={"class": "field-input", "placeholder": "mc_survival"}),
            "log_file_path":     forms.TextInput(attrs={"class": "field-input", "placeholder": "/srv/mc/logs/latest.log"}),
            "rcon_host":         forms.TextInput(attrs={"class": "field-input", "placeholder": "127.0.0.1"}),
            "rcon_port":         forms.NumberInput(attrs={"class": "field-input"}),
            "rcon_password":     forms.PasswordInput(attrs={"class": "field-input"}, render_value=True),
            "webhook_url":           forms.URLInput(attrs={"class": "field-input"}),
            "backup_directory":      forms.TextInput(attrs={"class": "field-input", "placeholder": "/srv/mc/backups"}),
            "backup_max_age_hours":  forms.NumberInput(attrs={"class": "field-input"}),
        }
        help_texts = {
            "slug":              "Automaticky odvozeno z názvu. Měň jen pokud potřebuješ vlastní URL identifikátor.",
            "start_command":     "Příkaz pro spuštění. Nechte prázdné pro použití start profilu.",
        }


class StartProfileForm(forms.ModelForm):
    """Start profil pro Minecraft JVM."""

    class Meta:
        model  = StartProfile
        fields = ["name", "java_path", "jar_file", "heap_min_mb", "heap_max_mb", "jvm_flags", "extra_args"]
        widgets = {
            "name":        forms.TextInput(attrs={"class": "field-input", "placeholder": "Výchozí profil"}),
            "java_path":   forms.TextInput(attrs={"class": "field-input field-mono", "placeholder": 'C:\\Program Files\\Java\\jdk-21\\bin\\java.exe'}),
            "jar_file":    forms.TextInput(attrs={"class": "field-input", "placeholder": "server.jar"}),
            "heap_min_mb": forms.NumberInput(attrs={"class": "field-input"}),
            "heap_max_mb": forms.NumberInput(attrs={"class": "field-input"}),
            "jvm_flags":   forms.Textarea(attrs={
                "class": "field-input field-mono", "rows": 4,
                "placeholder": "-XX:+UseG1GC\n-XX:+ParallelRefProcEnabled\n-XX:MaxGCPauseMillis=200",
            }),
            "extra_args":  forms.TextInput(attrs={"class": "field-input", "placeholder": "--nogui"}),
        }

    def clean(self):
        cleaned = super().clean()
        mn = cleaned.get("heap_min_mb", 0)
        mx = cleaned.get("heap_max_mb", 0)
        if mn and mx and mn > mx:
            raise forms.ValidationError("Xms nesmí být větší než Xmx.")
        return cleaned


class ScheduledRestartForm(forms.ModelForm):
    """Plánovaný restart serveru."""

    restart_time = forms.TimeField(
        label="Cas restartu",
        input_formats=["%H:%M"],
        widget=forms.TimeInput(attrs={"class": "field-input", "type": "time"}, format="%H:%M"),
    )
    weekdays = forms.MultipleChoiceField(
        label="Dny v tydnu",
        required=False,
        choices=[
            ("1", "Pondeli"),
            ("2", "Utery"),
            ("3", "Streda"),
            ("4", "Ctvrtek"),
            ("5", "Patek"),
            ("6", "Sobota"),
            ("0", "Nedele"),
        ],
        widget=forms.CheckboxSelectMultiple(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.advanced_cron_warning = ""

        if self.instance and self.instance.pk:
            parsed = self.instance.get_schedule_parts()
            if parsed:
                self.initial.setdefault("restart_time", dt_time(hour=parsed["hour"], minute=parsed["minute"]))
                self.initial.setdefault("weekdays", parsed["weekdays"])
            else:
                self.advanced_cron_warning = (
                    "Tento plan pouzival pokrocily cron vyraz. Po ulozeni se prevede na bezny cas + vybrane dny."
                )
        else:
            self.initial.setdefault("restart_time", dt_time(hour=4, minute=0))
            self.initial.setdefault("weekdays", list(ScheduledRestart.WEEKDAY_ORDER))

    class Meta:
        model  = ScheduledRestart
        fields = ["label", "is_active", "warn_minutes"]
        widgets = {
            "label":        forms.TextInput(attrs={"class": "field-input", "placeholder": "Nocni restart"}),
            "warn_minutes": forms.NumberInput(attrs={"class": "field-input"}),
        }
        help_texts = {
            "warn_minutes": "Kolik minut predem poslat varovani do konzole. 0 = bez varovani.",
        }

    def clean(self):
        cleaned = super().clean()
        restart_time = cleaned.get("restart_time")
        weekdays = cleaned.get("weekdays") or []

        if not restart_time:
            return cleaned

        if not weekdays:
            raise forms.ValidationError("Vyber alespon jeden den, kdy se ma restart spoustet.")

        ordered_days = [day_value for day_value in ScheduledRestart.WEEKDAY_ORDER if day_value in set(weekdays)]
        day_expr = "*" if len(ordered_days) == len(ScheduledRestart.WEEKDAY_ORDER) else ",".join(ordered_days)
        cleaned["cron_expression"] = f"{restart_time.minute} {restart_time.hour} * * {day_expr}"
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.cron_expression = self.cleaned_data["cron_expression"]
        if commit:
            instance.save()
        return instance
