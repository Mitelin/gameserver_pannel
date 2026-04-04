"""
apps/console/management/commands/run_console_tail.py  (fáze 4 – final)

Plná integrace:
  - Graceful shutdown (SIGTERM/SIGINT)
  - Log rotation / truncation detection
  - Startup pattern detection → ONLINE přechod
  - Player tracker (join/leave, player count, TPS)
  - Log pattern alert engine
"""
import os
import signal
import time
import logging
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from apps.servers.models import Server, ServerStatus
from apps.console.models import ConsoleLine

logger = logging.getLogger(__name__)
POLL_INTERVAL = 0.25
CHANNEL_GROUP = "server.{server_id}.console"


class LogTailer:
    def __init__(self, server):
        self.server = server
        self.path   = Path(server.log_file_path)
        self._fh    = None
        self._inode = None

    def open(self):
        try:
            self._fh    = open(self.path, "r", encoding="utf-8", errors="replace")
            self._inode = os.fstat(self._fh.fileno()).st_ino
            self._fh.seek(0, 2)
            return True
        except OSError:
            self._fh = self._inode = None
            return False

    def close(self):
        if self._fh:
            try: self._fh.close()
            except Exception: pass
            self._fh = None

    def read_new_lines(self):
        if self._fh is None:
            self.open(); return []
        try:
            if self.path.stat().st_ino != self._inode:
                logger.info("[%s] Log rotation", self.server.slug)
                self.close(); self.open(); return []
            if self.path.stat().st_size < self._fh.tell():
                logger.info("[%s] Log truncation", self.server.slug)
                self.close(); self.open(); return []
        except OSError:
            self.close(); return []
        lines = []
        try:
            while True:
                line = self._fh.readline()
                if not line: break
                lines.append(line.rstrip("\n"))
        except OSError as e:
            logger.warning("[%s] Chyba čtení: %s", self.server.slug, e)
            self.close()
        return lines


class Command(BaseCommand):
    help = "Console tailer – fáze 4 (plná integrace)."

    def handle(self, *args, **options):
        self.stdout.write("Console tailer (fáze 4) spuštěn.")
        self._running = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

        channel_layer = get_channel_layer()
        tailers = {}

        while self._running:
            active = list(
                Server.objects.filter(is_active=True)
                              .exclude(status=ServerStatus.OFFLINE)
                              .select_related("process_state")
            )
            active_slugs = {s.slug for s in active}

            for server in active:
                if server.slug not in tailers:
                    t = LogTailer(server)
                    if t.open():
                        tailers[server.slug] = t

            for slug in list(tailers):
                if slug not in active_slugs:
                    tailers[slug].close(); del tailers[slug]

            for server in active:
                tailer = tailers.get(server.slug)
                if not tailer: continue
                new_lines = tailer.read_new_lines()
                if not new_lines: continue

                # Bulk DB insert
                ConsoleLine.objects.bulk_create([
                    ConsoleLine(server=server, line=line, stream_type="stdout", source="log_tail")
                    for line in new_lines
                ])

                # last_log_line_at
                try:
                    state = server.process_state
                    state.last_log_line_at = timezone.now()
                    state.save(update_fields=["last_log_line_at"])
                except Exception: pass

                # WS push
                group = CHANNEL_GROUP.format(server_id=str(server.id))
                for line in new_lines:
                    try:
                        async_to_sync(channel_layer.group_send)(
                            group,
                            {"type": "console.line", "server_id": str(server.id),
                             "timestamp": timezone.now().isoformat(), "line": line}
                        )
                    except Exception as e:
                        logger.warning("WS push selhal: %s", e)

                # ── Fáze 4 integrace ──────────────────────────────────
                for line in new_lines:
                    # Player tracking
                    try:
                        from apps.servers.player_tracker import process_line_for_players
                        process_line_for_players(server, line)
                    except Exception as e:
                        logger.debug("player_tracker chyba: %s", e)

                    # Log pattern alerts
                    try:
                        from apps.alerts.engine import check_log_pattern_alert
                        check_log_pattern_alert(server, line)
                    except Exception as e:
                        logger.debug("alert engine chyba: %s", e)

                # Startup pattern detection
                self._check_startup(server, new_lines)

            time.sleep(POLL_INTERVAL)

        for t in tailers.values(): t.close()
        self.stdout.write("Console tailer ukončen.")

    def _shutdown(self, signum, frame):
        self.stdout.write(f"Tailer: signál {signum}, ukončuji...")
        self._running = False

    def _check_startup(self, server, lines):
        if server.status != ServerStatus.STARTING:
            return
        from apps.servers.adapters import get_adapter
        from apps.audit.models import AuditEvent
        adapter = get_adapter(server.game_type)
        for line in lines:
            if adapter.is_startup_complete(line):
                logger.info("[%s] Startup confirmed: %s", server.slug, line[:80])
                server.status = ServerStatus.ONLINE
                server.save(update_fields=["status", "updated_at"])
                try:
                    state = server.process_state
                    state.status = ServerStatus.ONLINE
                    state.consecutive_failures = 0
                    state.save(update_fields=["status", "consecutive_failures"])
                except Exception: pass
                AuditEvent.objects.create(
                    server=server, event_type="server.start.confirmed",
                    severity="info", message=f"Startup: {line[:100]}",
                )
                break
