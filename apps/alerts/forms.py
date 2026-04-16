"""
apps/alerts/forms.py

Formulář pro správu AlertRule.
"""
from django import forms
from .models import AlertRule, AlertConditionType, AlertChannel


class AlertRuleForm(forms.ModelForm):

    class Meta:
        model  = AlertRule
        fields = [
            "name", "is_active", "condition_type",
            "status_values", "threshold_value", "duration_minutes", "log_pattern",
            "channel", "webhook_url", "email_address", "message_template", "cooldown_minutes",
        ]
        widgets = {
            "name":             forms.TextInput(attrs={"class": "field-input"}),
            "condition_type":   forms.Select(attrs={"class": "field-input", "id": "id_condition_type"}),
            "status_values":    forms.TextInput(attrs={
                "class": "field-input",
                "placeholder": "CRASHED,UNKNOWN",
            }),
            "threshold_value":  forms.NumberInput(attrs={"class": "field-input", "step": "0.1"}),
            "duration_minutes": forms.NumberInput(attrs={"class": "field-input"}),
            "log_pattern":      forms.TextInput(attrs={
                "class": "field-input field-mono",
                "placeholder": "ERROR|WARN|OutOfMemoryError",
            }),
            "channel":          forms.Select(attrs={"class": "field-input", "id": "id_channel"}),
            "webhook_url":      forms.URLInput(attrs={"class": "field-input"}),
            "email_address":    forms.EmailInput(attrs={"class": "field-input", "placeholder": "admin@example.com"}),
            "message_template": forms.Textarea(attrs={"class": "field-input", "rows": 3}),
            "cooldown_minutes": forms.NumberInput(attrs={"class": "field-input"}),
        }
        help_texts = {
            "status_values":    "Pro status_change – čárkou oddělené statusy, např. CRASHED,UNKNOWN",
            "threshold_value":  "Pro cpu_threshold: % (např. 90). Pro ram_threshold: MB (např. 4096).",
            "duration_minutes": "Pro no_players: počet minut bez hráče.",
            "log_pattern":      "Pro log_pattern: regulární výraz hledaný v každém řádku logu.",
            "message_template": "Proměnné: {server_name}, {status}, {value}, {condition}, {details}",
            "cooldown_minutes": "Minimální interval mezi opakovanými alerty (minuty).",
            "email_address":    "Cílová emailová adresa (SMTP musí být nakonfigurováno v Nastavení).",
        }

    def clean(self):
        cleaned = super().clean()
        ct      = cleaned.get("condition_type")
        channel = cleaned.get("channel")

        if ct == AlertConditionType.STATUS_CHANGE and not cleaned.get("status_values"):
            self.add_error("status_values", "Zadej alespoň jeden status (např. CRASHED).")

        if ct in (AlertConditionType.CPU_THRESHOLD, AlertConditionType.RAM_THRESHOLD):
            if not cleaned.get("threshold_value"):
                self.add_error("threshold_value", "Zadej limit (pro CPU: %, pro RAM: MB).")

        if ct == AlertConditionType.NO_PLAYERS and not cleaned.get("duration_minutes"):
            self.add_error("duration_minutes", "Zadej počet minut.")

        if ct == AlertConditionType.LOG_PATTERN and not cleaned.get("log_pattern"):
            self.add_error("log_pattern", "Zadej regulární výraz.")

        if channel == AlertChannel.WEBHOOK and not cleaned.get("webhook_url"):
            self.add_error("webhook_url", "Webhook URL je povinné pro kanál Webhook.")

        if channel == AlertChannel.EMAIL and not cleaned.get("email_address"):
            self.add_error("email_address", "Email adresa je povinná pro kanál Email.")

        return cleaned
