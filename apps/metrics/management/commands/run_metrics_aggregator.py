"""
apps/metrics/management/commands/run_metrics_aggregator.py

Pomocný command pro ruční spuštění agregace nebo cleanup.

Použití:
  python manage.py run_metrics_aggregator --aggregate
  python manage.py run_metrics_aggregator --cleanup
  python manage.py run_metrics_aggregator --all
"""
from django.core.management.base import BaseCommand
from apps.servers.models import Server
from apps.metrics.aggregator import aggregate_to_minutes, aggregate_to_hours, run_retention_cleanup


class Command(BaseCommand):
    help = "Spustí agregaci metrik nebo retention cleanup."

    def add_arguments(self, parser):
        parser.add_argument("--aggregate", action="store_true", help="Agreguj samply do minut a hodin")
        parser.add_argument("--cleanup",   action="store_true", help="Smaž stará data")
        parser.add_argument("--all",       action="store_true", help="Oboje")

    def handle(self, *args, **options):
        do_agg     = options["aggregate"] or options["all"]
        do_cleanup = options["cleanup"]   or options["all"]

        if not do_agg and not do_cleanup:
            self.stdout.write("Použij --aggregate, --cleanup nebo --all")
            return

        if do_agg:
            servers = Server.objects.filter(is_active=True)
            for server in servers:
                m = aggregate_to_minutes(server)
                h = aggregate_to_hours(server)
                self.stdout.write(f"  {server.slug}: {m} minut, {h} hodin agregováno")

        if do_cleanup:
            result = run_retention_cleanup()
            self.stdout.write(
                f"Cleanup: raw={result['deleted_raw']} "
                f"minutes={result['deleted_minutes']} "
                f"consolelines={result['deleted_console']}"
            )
