"""
apps/servers/forms.py

Formuláře pro správu serverů a start profilů.
"""
from django import forms
from .models import Server, StartProfile, GameType


class ServerForm(forms.ModelForm):
    """Vytvoření / editace serveru."""

    class Meta:
        model  = Server
        fields = [
            "name", "slug", "game_type", "is_active",
            "working_directory", "start_command", "stop_command",
            "tmux_session_name", "log_file_path",
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
            "tmux_session_name": forms.TextInput(attrs={"class": "field-input"}),
            "log_file_path":     forms.TextInput(attrs={"class": "field-input", "placeholder": "/srv/mc/logs/latest.log"}),
            "rcon_host":         forms.TextInput(attrs={"class": "field-input", "placeholder": "127.0.0.1"}),
            "rcon_port":         forms.NumberInput(attrs={"class": "field-input"}),
            "rcon_password":     forms.PasswordInput(attrs={"class": "field-input"}, render_value=True),
            "webhook_url":           forms.URLInput(attrs={"class": "field-input"}),
            "backup_directory":      forms.TextInput(attrs={"class": "field-input", "placeholder": "/srv/mc/backups"}),
            "backup_max_age_hours":  forms.NumberInput(attrs={"class": "field-input"}),
        }
        help_texts = {
            "slug":              "URL identifikátor – jen malá písmena, čísla, pomlčky.",
            "tmux_session_name": "Unikátní název tmux session (např. mc-survival).",
            "start_command":     "Příkaz pro spuštění. Lze přepsat aktivním start profilem.",
        }


class StartProfileForm(forms.ModelForm):
    """Start profil pro Minecraft JVM."""

    class Meta:
        model  = StartProfile
        fields = ["name", "jar_file", "heap_min_mb", "heap_max_mb", "jvm_flags", "extra_args"]
        widgets = {
            "name":        forms.TextInput(attrs={"class": "field-input", "placeholder": "Výchozí profil"}),
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
