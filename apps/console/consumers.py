"""
apps/console/consumers.py

Jeden WebSocket endpoint pro všechno:
  ws://host/ws/servers/<slug>/

Multiplexuje message types:
    • console.line      – nový řádek z živé konzole
  • server.status     – změna stavu serveru
  • metrics.snapshot  – aktuální metriky (každých 10s)
  • audit.event       – systémové eventy
  • command.update    – lifecycle příkazu

Klient posílá:
  { "type": "console.command", "command": "say hello" }
  { "type": "ping" }
"""
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

from apps.servers.models import Server
from apps.users.permissions import can_control_server, can_view_server

logger = logging.getLogger(__name__)

class ServerConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.slug       = self.scope["url_route"]["kwargs"]["slug"]
        self.user       = self.scope.get("user", AnonymousUser())
        self.server     = await self._get_server(self.slug)

        if self.server is None:
            await self.close(code=4004)
            return

        if not self.user.is_authenticated:
            await self.close(code=4003)
            return

        if not await database_sync_to_async(can_view_server)(self.user, self.server):
            await self.close(code=4003)
            return

        self.server_id = str(self.server.id)

        # Přihlásit se do všech skupin pro tento server
        self.groups = [
            f"server.{self.server_id}.console",
            f"server.{self.server_id}.status",
            f"server.{self.server_id}.metrics",
            f"server.{self.server_id}.events",
        ]
        for group in self.groups:
            await self.channel_layer.group_add(group, self.channel_name)

        await self.accept()
        logger.info("WS connect: user=%s server=%s", self.user, self.slug)

        # Zajisti direct console capture pokud proces běží a relay v paměti ještě neběží.
        await database_sync_to_async(self._ensure_console_capture)()

        # Aktuální stav serveru
        await self._send_status_snapshot()
        await self._send_console_state()

    async def disconnect(self, close_code):
        for group in getattr(self, "groups", []):
            await self.channel_layer.group_discard(group, self.channel_name)
        logger.info("WS disconnect: user=%s server=%s code=%s", self.user, self.slug, close_code)

    # ─────────────────────────────────────────────
    # Příchozí zprávy od klienta
    # ─────────────────────────────────────────────

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Neplatný JSON.")
            return

        msg_type = data.get("type", "")

        if msg_type == "console.command":
            await self._handle_command(data.get("command", ""))
        elif msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
        else:
            await self._send_error(f"Neznámý typ zprávy: {msg_type}")

    async def _handle_command(self, command: str):
        if not await database_sync_to_async(can_control_server)(self.user, self.server):
            await self.send(text_data=json.dumps({
                "type": "command.update",
                "ok": False,
                "message": "Přístup odepřen.",
                "status": "FAILED",
            }))
            return

        from apps.control.service import send_console_command
        result = await database_sync_to_async(send_console_command)(
            self.server, command, self.user
        )
        await self.send(text_data=json.dumps({
            "type":    "command.update",
            "ok":      result["ok"],
            "message": result["message"],
            "status":  "DISPATCHED" if result["ok"] else "FAILED",
        }))

    # ─────────────────────────────────────────────
    # Zprávy z channel layer (group_send z workerů)
    # ─────────────────────────────────────────────

    async def console_line(self, event):
        """Handler pro type=console.line (z živé konzole)."""
        await self.send(text_data=json.dumps({
            "type":      "console.line",
            "timestamp": event["timestamp"],
            "line":      event["line"],
        }))

    async def server_status(self, event):
        """Handler pro type=server.status (z watchdogu)."""
        await self.send(text_data=json.dumps({
            "type":   "server.status",
            "status": event["status"],
        }))

    async def metrics_snapshot(self, event):
        """Handler pro type=metrics.snapshot (z metrics collectoru)."""
        await self.send(text_data=json.dumps({
            "type":        "metrics.snapshot",
            "cpu_percent": event.get("cpu_percent"),
            "ram_bytes":   event.get("ram_bytes"),
            "players":     event.get("players"),
            "threads":     event.get("threads"),
            "timestamp":   event.get("timestamp"),
        }))

    async def audit_event(self, event):
        """Handler pro type=audit.event."""
        await self.send(text_data=json.dumps({
            "type":       "audit.event",
            "event_type": event.get("event_type"),
            "severity":   event.get("severity"),
            "message":    event.get("message"),
            "timestamp":  event.get("timestamp"),
        }))

    async def console_state(self, event):
        await self.send(text_data=json.dumps({
            "type": "console.state",
            "available": event.get("available", False),
            "can_write": event.get("can_write", False),
            "message": event.get("message", ""),
        }))

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────

    async def _send_status_snapshot(self):
        server = await database_sync_to_async(
            lambda: Server.objects.select_related("process_state").get(pk=self.server.pk)
        )()
        await self.send(text_data=json.dumps({
            "type":   "server.status",
            "status": server.status,
        }))

    async def _send_console_state(self):
        state = await database_sync_to_async(self._get_console_state)()
        await self.send(text_data=json.dumps({
            "type": "console.state",
            "available": state.get("available", False),
            "can_write": state.get("can_write", False),
            "message": state.get("message", ""),
        }))

    async def _send_error(self, message: str):
        await self.send(text_data=json.dumps({"type": "error", "message": message}))

    def _ensure_console_capture(self):
        """Zajistí přímé čtení stdout/stderr procesu pro web konzoli."""
        try:
            from apps.servers.models import ServerStatus
            from apps.control.service import backend
            if self.server.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
                ensure = getattr(backend, "ensure_console_capture", None)
                if ensure is not None:
                    ensure(self.server)
        except Exception as e:
            logger.debug("ensure_console_capture: %s", e)

    def _get_console_state(self):
        try:
            from apps.control.service import backend
            get_state = getattr(backend, "get_console_state", None)
            if get_state is not None:
                return get_state(self.server)
        except Exception as e:
            logger.debug("get_console_state: %s", e)
        return {"available": False, "can_write": False, "message": "Stav konzole se nepodařilo zjistit."}

    @database_sync_to_async
    def _get_server(self, slug: str):
        try:
            return Server.objects.get(slug=slug, is_active=True)
        except Server.DoesNotExist:
            return None
