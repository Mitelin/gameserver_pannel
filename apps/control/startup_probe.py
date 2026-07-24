import random
import socket
import struct
from pathlib import Path

from apps.console.buffer import get_console_lines
from apps.servers.adapters import get_adapter
from apps.servers.discovery import read_properties_file
from apps.servers.models import GameType


BEDROCK_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")


def _startup_log_ready(server) -> bool:
    adapter = get_adapter(server.game_type)
    patterns = adapter.startup_patterns()
    if not patterns:
        return False
    recent = get_console_lines(server.id, 100, live_only=True)
    return any(adapter.is_startup_complete(item["line"]) for item in recent)


def _read_server_props(server) -> dict[str, str]:
    working_directory = (server.working_directory or "").strip()
    if not working_directory:
        return {}
    return read_properties_file(Path(working_directory) / "server.properties")


def _normalize_host(value: str | None, fallback: str = "127.0.0.1") -> str:
    host = (value or "").strip()
    if not host or host in {"0.0.0.0", "::", "*"}:
        return fallback
    return host


def _parse_port(value: str | int | None, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _minecraft_java_target(server):
    props = _read_server_props(server)
    host = _normalize_host(props.get("server-ip") or server.rcon_host or "127.0.0.1")
    port = _parse_port(props.get("server-port"), 25565)
    return host, port


def _minecraft_bedrock_target(server):
    props = _read_server_props(server)
    host = _normalize_host(props.get("server-ip") or server.rcon_host or "127.0.0.1")
    port = _parse_port(props.get("server-port"), 19132)
    return host, port


def _probe_tcp(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_bedrock_udp(host: str, port: int, timeout: float = 1.2) -> bool:
    packet = b"\x01" + struct.pack(">Q", 0) + BEDROCK_MAGIC + struct.pack(">Q", random.getrandbits(64))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (host, port))
            data, _ = sock.recvfrom(2048)
            return bool(data) and data[:1] == b"\x1c"
    except OSError:
        return False


def probe_server_connectable(server) -> bool | None:
    if server.game_type == GameType.MINECRAFT_JAVA:
        host, port = _minecraft_java_target(server)
        return _probe_tcp(host, port)
    if server.game_type == GameType.MINECRAFT_BEDROCK:
        host, port = _minecraft_bedrock_target(server)
        return _probe_bedrock_udp(host, port)
    return None


def is_startup_ready(server) -> bool:
    connectable = probe_server_connectable(server)
    if connectable is not None:
        return connectable
    return _startup_log_ready(server)