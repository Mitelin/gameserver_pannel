"""
apps/setup/forms.py – formuláře pro setup wizard a settings stránku
"""
from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

User = get_user_model()

_TIMEZONES = [
    ("Europe/Prague",       "Europe/Prague"),
    ("Europe/London",       "Europe/London"),
    ("Europe/Berlin",       "Europe/Berlin"),
    ("Europe/Warsaw",       "Europe/Warsaw"),
    ("Europe/Bratislava",   "Europe/Bratislava"),
    ("UTC",                 "UTC"),
    ("America/New_York",    "America/New_York"),
    ("America/Chicago",     "America/Chicago"),
    ("America/Denver",      "America/Denver"),
    ("America/Los_Angeles", "America/Los_Angeles"),
    ("Asia/Tokyo",          "Asia/Tokyo"),
    ("Asia/Shanghai",       "Asia/Shanghai"),
]

_LANGUAGES = [
    ("cs", "Čeština"),
    ("en", "English"),
]


class AdminAccountForm(forms.Form):
    username  = forms.CharField(
        max_length=150, label="Uživatelské jméno",
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )
    email     = forms.EmailField(
        label="E-mail", required=False,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    password  = forms.CharField(
        label="Heslo", min_length=8,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Heslo znovu",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise ValidationError("Uživatel s tímto jménem již existuje.")
        return username

    def clean(self):
        cleaned = super().clean()
        pw1 = cleaned.get("password")
        pw2 = cleaned.get("password2")
        if pw1 and pw2 and pw1 != pw2:
            self.add_error("password2", "Hesla se neshodují.")
        return cleaned


class WizardStep3Form(forms.Form):
    """Formulář pro krok 3 wizardu – pouze základní nastavení panelu."""

    # ── Obecné ────────────────────────────────────────────────────────────
    panel_name      = forms.CharField(
        max_length=128, label="Název panelu", initial="Gameserver Panel",
    )
    public_base_url = forms.CharField(
        max_length=256, label="Veřejná URL", required=False,
        help_text="Např. https://panel.example.com – volitelné",
    )
    timezone        = forms.ChoiceField(
        choices=_TIMEZONES, label="Časová zóna", initial="Europe/Prague",
    )
    default_language = forms.ChoiceField(
        choices=_LANGUAGES, label="Jazyk", initial="cs",
    )

    # ── Timeouty ──────────────────────────────────────────────────────────
    default_startup_timeout = forms.IntegerField(
        label="Timeout startu serveru (s)", min_value=10, max_value=600, initial=60,
    )
    default_shutdown_timeout = forms.IntegerField(
        label="Timeout vypnutí serveru (s)", min_value=5, max_value=300, initial=30,
    )

    # ── Retence ───────────────────────────────────────────────────────────
    raw_metrics_retention_days    = forms.IntegerField(
        label="Raw metriky (dny)", min_value=1, max_value=90, initial=7,
    )
    minute_metrics_retention_days = forms.IntegerField(
        label="Minutové metriky (dny)", min_value=1, max_value=365, initial=60,
    )
    console_retention_days        = forms.IntegerField(
        label="Konzole logy (dny)", min_value=1, max_value=90, initial=14,
    )


class SystemSettingsForm(forms.Form):
    """Plný formulář pro /settings/ stránku – všechna nastavení."""

    # ── Obecné ────────────────────────────────────────────────────────────
    panel_name      = forms.CharField(
        max_length=128, label="Název panelu", initial="Gameserver Panel",
    )
    public_base_url = forms.CharField(
        max_length=256, label="Veřejná URL", required=False,
        help_text="Např. https://panel.example.com – volitelné",
    )
    timezone        = forms.ChoiceField(
        choices=_TIMEZONES, label="Časová zóna", initial="Europe/Prague",
    )
    default_language = forms.ChoiceField(
        choices=_LANGUAGES, label="Jazyk", initial="cs",
    )

    # ── Timeouty ──────────────────────────────────────────────────────────
    default_startup_timeout = forms.IntegerField(
        label="Timeout startu serveru (s)", min_value=10, max_value=600, initial=60,
    )
    default_shutdown_timeout = forms.IntegerField(
        label="Timeout vypnutí serveru (s)", min_value=5, max_value=300, initial=30,
    )

    # ── Retence ───────────────────────────────────────────────────────────
    raw_metrics_retention_days    = forms.IntegerField(
        label="Raw metriky (dny)", min_value=1, max_value=90, initial=7,
    )
    minute_metrics_retention_days = forms.IntegerField(
        label="Minutové metriky (dny)", min_value=1, max_value=365, initial=60,
    )
    console_retention_days        = forms.IntegerField(
        label="Konzole logy (dny)", min_value=1, max_value=90, initial=14,
    )

    # ── Runtime polling ───────────────────────────────────────────────────
    runtime_poll_console_ms       = forms.IntegerField(
        label="Konzole tail interval (ms)", min_value=100, max_value=5000, initial=500,
    )
    runtime_poll_metrics_seconds  = forms.IntegerField(
        label="Metriky interval (s)", min_value=5, max_value=120, initial=12,
    )
    runtime_poll_watchdog_seconds = forms.IntegerField(
        label="Watchdog interval (s)", min_value=5, max_value=120, initial=15,
    )

    # ── Alerting ──────────────────────────────────────────────────────────
    alert_cooldown_minutes = forms.IntegerField(
        label="Alert cooldown (minuty)", min_value=1, max_value=60, initial=15,
    )

    # ── SMTP ──────────────────────────────────────────────────────────────
    smtp_enabled      = forms.BooleanField(label="Povolit emailové alerty", required=False)
    smtp_host         = forms.CharField(
        label="SMTP host", max_length=256, required=False,
        help_text="Např. smtp.gmail.com nebo smtp.sendgrid.net",
        widget=forms.TextInput(attrs={"placeholder": "smtp.gmail.com"}),
    )
    smtp_port         = forms.IntegerField(
        label="SMTP port", initial=587, min_value=1, max_value=65535,
        help_text="587 = STARTTLS, 465 = SSL, 25 = plain",
    )
    smtp_use_tls      = forms.BooleanField(label="Použít STARTTLS (port 587)", required=False, initial=True)
    smtp_use_ssl      = forms.BooleanField(label="Použít SSL (port 465)", required=False)
    smtp_username     = forms.CharField(label="SMTP uživatel", max_length=256, required=False)
    smtp_password     = forms.CharField(
        label="SMTP heslo", max_length=256, required=False,
        widget=forms.PasswordInput(render_value=True),
    )
    smtp_from_address = forms.EmailField(
        label="Odesílací adresa", required=False,
        help_text="Např. panel@example.com",
        widget=forms.EmailInput(attrs={"placeholder": "panel@example.com"}),
    )

    # Pole zobrazená ve wizardu (krok 3) – ostatní jsou jen v /settings/
    WIZARD_FIELDS = [
        "panel_name", "public_base_url", "timezone", "default_language",
        "default_startup_timeout", "default_shutdown_timeout",
        "raw_metrics_retention_days", "minute_metrics_retention_days",
        "console_retention_days",
    ]


class FirstServerForm(forms.Form):
    name              = forms.CharField(
        max_length=128, label="Název serveru",
        help_text="Např. Minecraft Prod",
    )
    slug              = forms.SlugField(
        max_length=64, label="Slug (URL identifikátor)",
        help_text="Pouze písmena, čísla a pomlčky – např. mc-prod",
    )
    game_type         = forms.ChoiceField(choices=[], label="Typ hry")
    working_directory = forms.CharField(
        max_length=512, label="Pracovní adresář",
        help_text="Absolutní cesta k adresáři serveru",
    )
    start_command     = forms.CharField(
        max_length=512, label="Start příkaz",
        help_text="Např. java -Xmx4G -jar server.jar nogui",
    )
    stop_command      = forms.CharField(
        max_length=512, label="Stop příkaz", required=False,
        help_text="Např. stop – volitelné",
    )
    tmux_session_name = forms.CharField(
        max_length=64, label="tmux session název",
        help_text="Unikátní název tmux session pro tento server",
    )
    log_file_path     = forms.CharField(
        max_length=512, label="Cesta k log souboru",
        help_text="Např. /srv/mc-prod/logs/latest.log",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.servers.models import GameType
        self.fields["game_type"].choices = GameType.choices

    def clean_slug(self):
        from apps.servers.models import Server
        slug = self.cleaned_data["slug"]
        if Server.objects.filter(slug=slug).exists():
            raise ValidationError("Server s tímto slugem již existuje.")
        return slug

    def clean_tmux_session_name(self):
        from apps.servers.models import Server
        name = self.cleaned_data["tmux_session_name"]
        if Server.objects.filter(tmux_session_name=name).exists():
            raise ValidationError("tmux session s tímto názvem již existuje.")
        return name
