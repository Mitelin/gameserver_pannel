"""
apps/common/management/commands/run_retention_cleanup.py

Ruční nebo cron-based cleanup starých dat.

Použití:
  python manage.py run_retention_cleanup
  python manage.py run_retention_cleanup --dry-run
    python manage.py run_retention_cleanup --raw-days=3 --audit-days=30
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):
    help = "Smaže stará data dle retention policy."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run",      action="store_true", help="Pouze zobraz co by bylo smazáno")
        parser.add_argument("--raw-days",     type=int, default=7,  help="Retention raw MetricSample (dní)")
        parser.add_argument("--minute-days",  type=int, default=60, help="Retention MetricMinute (dní)")
        parser.add_argument("--audit-days",   type=int, default=0,  help="Retention AuditEvent (dní, 0=navždy)")

    def handle(self, *args, **options):
        dry     = options["dry_run"]
        now     = timezone.now()
        label   = "[DRY-RUN] " if dry else ""

        cutoffs = {
            "MetricSample":  now - timedelta(days=options["raw_days"]),
            "MetricMinute":  now - timedelta(days=options["minute_days"]),
        }
        if options["audit_days"] > 0:
            cutoffs["AuditEvent"] = now - timedelta(days=options["audit_days"])

        from apps.metrics.models import MetricSample, MetricMinute
        from apps.audit.models   import AuditEvent

        model_map = {
            "MetricSample": MetricSample,
            "MetricMinute": MetricMinute,
            "AuditEvent":   AuditEvent,
        }

        total = 0
        for name, cutoff in cutoffs.items():
            model = model_map[name]
            qs    = model.objects.filter(timestamp__lt=cutoff)
            count = qs.count()
            total += count
            self.stdout.write(
                f"  {label}{name}: {count} záznamů starších než {cutoff.strftime('%d.%m.%Y %H:%M')}"
            )
            if not dry and count > 0:
                qs.delete()
                self.stdout.write(f"    → smazáno {count} záznamů")

        if dry:
            self.stdout.write(f"\n[DRY-RUN] Celkem by bylo smazáno: {total} záznamů")
            self.stdout.write("Spusť bez --dry-run pro skutečné smazání.")
        else:
            self.stdout.write(f"\nCelkem smazáno: {total} záznamů")
