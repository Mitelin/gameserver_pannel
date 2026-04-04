"""
apps/control/backends/rcon.py

RCON klient pro Minecraft Java (Source RCON protocol).
Používá se jako alternativa k tmux send-keys pro posílání příkazů.

Výhody RCON oproti tmux:
  - Přímá odpověď na příkaz (output v response)
  - Funguje i pokud není tmux session dostupná lokálně
  - Čistší příkazy bez side-effectů terminálu

Použití:
    from apps.control.backends.rcon import RconClient, RconError
    with RconClient(host, port, password) as rcon:
        response = rcon.command("list")
        print(response)  # "There are 2 of a max of 20 players online"
"""
import socket
import struct
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Source RCON packet types
SERVERDATA_AUTH         = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND  = 2
SERVERDATA_RESPONSE_VALUE = 0

RCON_TIMEOUT = 5   # sekund


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


class RconClient:
    """
    Source RCON protokol klient.
    Používej jako context manager: `with RconClient(...) as rcon:`
    """

    def __init__(self, host: str, port: int, password: str):
        self.host     = host
        self.port     = port
        self.password = password
        self._sock    = None
        self._req_id  = 1

    def connect(self):
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=RCON_TIMEOUT
            )
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            raise RconError(f"Nelze se připojit k RCON {self.host}:{self.port}: {exc}") from exc

        # Autentizace
        auth_id = self._send_packet(SERVERDATA_AUTH, self.password)
        ptype, pid, _ = self._recv_packet()

        if pid == -1 or ptype != SERVERDATA_AUTH_RESPONSE:
            raise RconAuthError("RCON autentizace selhala – špatné heslo?")

        logger.debug("RCON připojeno: %s:%d", self.host, self.port)

    def command(self, cmd: str) -> str:
        """Pošle příkaz a vrátí odpověď serveru."""
        if not self._sock:
            raise RconError("RCON není připojeno.")

        req_id = self._send_packet(SERVERDATA_EXECCOMMAND, cmd)

        # Minecraft může posílat více paketů pro dlouhé odpovědi
        # Posíláme prázdný druhý paket jako "sentinel"
        sentinel_id = self._send_packet(SERVERDATA_EXECCOMMAND, "")

        response_parts = []
        while True:
            ptype, pid, payload = self._recv_packet()
            if pid == sentinel_id:
                break
            if pid == req_id:
                response_parts.append(payload)

        return "".join(response_parts)

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ─── Interní protokol ────────────────────────────────────

    def _send_packet(self, ptype: int, payload: str) -> int:
        req_id = self._req_id
        self._req_id += 1

        payload_bytes = payload.encode("utf-8") + b"\x00\x00"
        # length = req_id(4) + type(4) + payload + null terminators
        length = 4 + 4 + len(payload_bytes)

        packet = struct.pack("<iii", length, req_id, ptype) + payload_bytes
        try:
            self._sock.sendall(packet)
        except OSError as exc:
            raise RconError(f"Chyba odesílání RCON paketu: {exc}") from exc

        return req_id

    def _recv_packet(self) -> tuple[int, int, str]:
        try:
            raw_len = self._recv_bytes(4)
            length  = struct.unpack("<i", raw_len)[0]
            data    = self._recv_bytes(length)
        except OSError as exc:
            raise RconError(f"Chyba příjmu RCON paketu: {exc}") from exc

        req_id = struct.unpack("<i", data[0:4])[0]
        ptype  = struct.unpack("<i", data[4:8])[0]
        # Payload je mezi byte 8 a length-2 (bez dvou null terminátorů)
        payload = data[8:-2].decode("utf-8", errors="replace")
        return ptype, req_id, payload

    def _recv_bytes(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("RCON spojení přerušeno.")
            buf += chunk
        return buf


# ─── Convenience funkce ──────────────────────────────────────

def rcon_command(host: str, port: int, password: str, command: str) -> str:
    """Jednorázový RCON příkaz bez nutnosti spravovat spojení."""
    with RconClient(host, port, password) as rcon:
        return rcon.command(command)


def rcon_player_list(host: str, port: int, password: str) -> list[str]:
    """Vrátí seznam online hráčů přes RCON list příkaz."""
    response = rcon_command(host, port, password, "list")
    # Response: "There are 2 of a max of 20 players online: Steve, Alex"
    if ":" in response:
        players_str = response.split(":", 1)[1].strip()
        if players_str:
            return [p.strip() for p in players_str.split(",") if p.strip()]
    return []
