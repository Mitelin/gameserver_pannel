"""
apps/control/mcadmin.py

Minecraft whitelist/ban správa přes RCON nebo přímou editaci JSON souborů.

Preferuje RCON pokud je povoleno; fallback na soubory whitelist.json / banned-players.json
v server.working_directory.
"""
import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

LEGACY_WHITELIST_FILE = "white-list.txt"
MODERN_WHITELIST_FILE = "whitelist.json"
LEGACY_BAN_FILE = "banned-players.txt"
MODERN_BAN_FILE = "banned-players.json"
LEGACY_IP_BAN_FILE = "banned-ips.txt"
MODERN_IP_BAN_FILE = "banned-ips.json"


def _rcon_command(server, cmd: str) -> tuple[bool, str]:
    """Pošle RCON příkaz. Vrátí (ok, response)."""
    try:
        from mcrcon import MCRcon
        with MCRcon(
            server.rcon_host or "127.0.0.1",
            server.rcon_password,
            port=server.rcon_port or 25575,
        ) as rcon:
            resp = rcon.command(cmd)
            return True, resp
    except Exception as exc:
        logger.warning("RCON selhal: %s", exc)
        return False, str(exc)


def _server_path(server, filename: str) -> Path:
    return Path(server.working_directory) / filename


def _uses_legacy_player_files(server) -> bool:
    legacy_markers = [LEGACY_WHITELIST_FILE, LEGACY_BAN_FILE, LEGACY_IP_BAN_FILE]
    return any(_server_path(server, name).exists() for name in legacy_markers)


def _resolve_whitelist_file(server) -> tuple[str, str]:
    legacy_path = _server_path(server, LEGACY_WHITELIST_FILE)
    modern_path = _server_path(server, MODERN_WHITELIST_FILE)
    if legacy_path.exists():
        return "legacy", LEGACY_WHITELIST_FILE
    if modern_path.exists():
        return "json", MODERN_WHITELIST_FILE
    if _uses_legacy_player_files(server):
        return "legacy", LEGACY_WHITELIST_FILE
    return "json", MODERN_WHITELIST_FILE


def _resolve_ban_file(server) -> tuple[str, str]:
    legacy_path = _server_path(server, LEGACY_BAN_FILE)
    modern_path = _server_path(server, MODERN_BAN_FILE)
    if legacy_path.exists():
        return "legacy", LEGACY_BAN_FILE
    if modern_path.exists():
        return "json", MODERN_BAN_FILE
    if _uses_legacy_player_files(server):
        return "legacy", LEGACY_BAN_FILE
    return "json", MODERN_BAN_FILE


def _resolve_ip_ban_file(server) -> tuple[str, str]:
    legacy_path = _server_path(server, LEGACY_IP_BAN_FILE)
    modern_path = _server_path(server, MODERN_IP_BAN_FILE)
    if legacy_path.exists():
        return "legacy", LEGACY_IP_BAN_FILE
    if modern_path.exists():
        return "json", MODERN_IP_BAN_FILE
    if _uses_legacy_player_files(server):
        return "legacy", LEGACY_IP_BAN_FILE
    return "json", MODERN_IP_BAN_FILE


def _read_json_file(server, filename: str) -> list[dict]:
    path = _server_path(server, filename)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Nelze číst %s: %s", filename, exc)
        return []


def _write_json_file(server, filename: str, data: list):
    path = _server_path(server, filename)
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Nelze zapsat %s: %s", filename, exc)
        return False


def _read_legacy_whitelist(server) -> list[dict]:
    path = _server_path(server, LEGACY_WHITELIST_FILE)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        logger.warning("Nelze číst %s: %s", LEGACY_WHITELIST_FILE, exc)
        return []

    result = []
    for line in lines:
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        result.append({"uuid": "", "name": name})
    return result


def _write_legacy_whitelist(server, entries: list[dict]) -> bool:
    path = _server_path(server, LEGACY_WHITELIST_FILE)
    names = []
    seen = set()
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        lowered = name.lower()
        if not name or lowered in seen:
            continue
        seen.add(lowered)
        names.append(name)

    content = "\n".join(names)
    if content:
        content += "\n"
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Nelze zapsat %s: %s", LEGACY_WHITELIST_FILE, exc)
        return False


def _read_legacy_bans(server) -> list[dict]:
    path = _server_path(server, LEGACY_BAN_FILE)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        logger.warning("Nelze číst %s: %s", LEGACY_BAN_FILE, exc)
        return []

    entries = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        parts += [""] * (5 - len(parts))
        entries.append({
            "uuid": "",
            "name": parts[0],
            "created": parts[1],
            "source": parts[2],
            "expires": parts[3],
            "reason": parts[4],
        })
    return entries


def _legacy_ban_header(server) -> list[str]:
    path = _server_path(server, LEGACY_BAN_FILE)
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            header = [line for line in lines if line.strip().startswith("#")]
            if header:
                return header
        except Exception as exc:
            logger.warning("Nelze načíst hlavičku %s: %s", LEGACY_BAN_FILE, exc)

    stamp = datetime.now().strftime("%d.%m.%y %H:%M")
    return [
        f"# Updated {stamp} by GamePanel",
        "# victim name | ban date | banned by | banned until | reason",
    ]


def _legacy_ip_ban_header(server) -> list[str]:
    path = _server_path(server, LEGACY_IP_BAN_FILE)
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            header = [line for line in lines if line.strip().startswith("#")]
            if header:
                return header
        except Exception as exc:
            logger.warning("Nelze načíst hlavičku %s: %s", LEGACY_IP_BAN_FILE, exc)

    stamp = datetime.now().strftime("%d.%m.%y %H:%M")
    return [
        f"# Updated {stamp} by GamePanel",
        "# victim name | ban date | banned by | banned until | reason",
    ]


def _write_legacy_bans(server, entries: list[dict]) -> bool:
    path = _server_path(server, LEGACY_BAN_FILE)
    header = _legacy_ban_header(server)
    lines = []
    seen = set()
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        lowered = name.lower()
        if not name or lowered in seen:
            continue
        seen.add(lowered)
        created = str(entry.get("created") or datetime.now().strftime("%d.%m.%y %H:%M")).strip()
        source = str(entry.get("source") or "Server").strip()
        expires = str(entry.get("expires") or "forever").strip()
        reason = str(entry.get("reason") or "Banned by admin").strip()
        lines.append(f"{name} | {created} | {source} | {expires} | {reason}")

    content = "\n".join([*header, *lines])
    if content:
        content += "\n"
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Nelze zapsat %s: %s", LEGACY_BAN_FILE, exc)
        return False


def _read_legacy_ip_bans(server) -> list[dict]:
    path = _server_path(server, LEGACY_IP_BAN_FILE)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        logger.warning("Nelze číst %s: %s", LEGACY_IP_BAN_FILE, exc)
        return []

    entries = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        parts += [""] * (5 - len(parts))
        entries.append({
            "ip": parts[0],
            "created": parts[1],
            "source": parts[2],
            "expires": parts[3],
            "reason": parts[4],
        })
    return entries


def _write_legacy_ip_bans(server, entries: list[dict]) -> bool:
    path = _server_path(server, LEGACY_IP_BAN_FILE)
    header = _legacy_ip_ban_header(server)
    lines = []
    seen = set()
    for entry in entries:
        ip = str(entry.get("ip", "")).strip()
        if not ip or ip in seen:
            continue
        seen.add(ip)
        created = str(entry.get("created") or datetime.now().strftime("%d.%m.%y %H:%M")).strip()
        source = str(entry.get("source") or "Server").strip()
        expires = str(entry.get("expires") or "forever").strip()
        reason = str(entry.get("reason") or "Banned by admin").strip()
        lines.append(f"{ip} | {created} | {source} | {expires} | {reason}")

    content = "\n".join([*header, *lines])
    if content:
        content += "\n"
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Nelze zapsat %s: %s", LEGACY_IP_BAN_FILE, exc)
        return False


# ── Whitelist ────────────────────────────────────────────────────────────────

def whitelist_list(server) -> list[dict]:
    storage, filename = _resolve_whitelist_file(server)
    if storage == "legacy":
        return _read_legacy_whitelist(server)
    return _read_json_file(server, filename)


def whitelist_add(server, player_name: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"whitelist add {player_name}")
        if ok:
            return True, resp or f"{player_name} přidán na whitelist."
    storage, filename = _resolve_whitelist_file(server)
    entries = whitelist_list(server)
    if any(e.get("name", "").lower() == player_name.lower() for e in entries):
        return False, f"{player_name} je již na whitelistu."
    entries.append({"uuid": "", "name": player_name})
    ok = _write_legacy_whitelist(server, entries) if storage == "legacy" else _write_json_file(server, filename, entries)
    return (True, f"{player_name} přidán.") if ok else (False, "Chyba zápisu souboru.")


def whitelist_remove(server, player_name: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"whitelist remove {player_name}")
        if ok:
            return True, resp or f"{player_name} odebrán z whitelistu."
    storage, filename = _resolve_whitelist_file(server)
    entries = [e for e in whitelist_list(server) if e.get("name", "").lower() != player_name.lower()]
    ok = _write_legacy_whitelist(server, entries) if storage == "legacy" else _write_json_file(server, filename, entries)
    return (True, f"{player_name} odebrán.") if ok else (False, "Chyba zápisu souboru.")


# ── Ban list ─────────────────────────────────────────────────────────────────

def ban_list(server) -> list[dict]:
    storage, filename = _resolve_ban_file(server)
    if storage == "legacy":
        return _read_legacy_bans(server)
    return _read_json_file(server, filename)


def ban_add(server, player_name: str, reason: str = "Banned by admin") -> tuple[bool, str]:
    if server.rcon_enabled:
        cmd = f"ban {player_name} {reason}" if reason else f"ban {player_name}"
        ok, resp = _rcon_command(server, cmd)
        if ok:
            return True, resp or f"{player_name} zabanován."
    storage, filename = _resolve_ban_file(server)
    entries = ban_list(server)
    if any(e.get("name", "").lower() == player_name.lower() for e in entries):
        return False, f"{player_name} je již zabanován."
    entries.append({
        "uuid":    "",
        "name":    player_name,
        "created": datetime.now().strftime("%d.%m.%y %H:%M") if storage == "legacy" else datetime.now().strftime("%Y-%m-%d %H:%M:%S +0000"),
        "source":  "Server",
        "expires": "forever",
        "reason":  reason,
    })
    ok = _write_legacy_bans(server, entries) if storage == "legacy" else _write_json_file(server, filename, entries)
    return (True, f"{player_name} zabanován.") if ok else (False, "Chyba zápisu souboru.")


def ban_remove(server, player_name: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"pardon {player_name}")
        if ok:
            return True, resp or f"{player_name} odbanován."
    storage, filename = _resolve_ban_file(server)
    entries = [e for e in ban_list(server) if e.get("name", "").lower() != player_name.lower()]
    ok = _write_legacy_bans(server, entries) if storage == "legacy" else _write_json_file(server, filename, entries)
    return (True, f"{player_name} odbanován.") if ok else (False, "Chyba zápisu souboru.")


def ip_ban_list(server) -> list[dict]:
    storage, filename = _resolve_ip_ban_file(server)
    if storage == "legacy":
        return _read_legacy_ip_bans(server)
    return _read_json_file(server, filename)


def ip_ban_add(server, ip_address: str, reason: str = "Banned by admin") -> tuple[bool, str]:
    if server.rcon_enabled:
        cmd = f"ban-ip {ip_address} {reason}" if reason else f"ban-ip {ip_address}"
        ok, resp = _rcon_command(server, cmd)
        if ok:
            return True, resp or f"IP {ip_address} zabanována."
    storage, filename = _resolve_ip_ban_file(server)
    entries = ip_ban_list(server)
    if any(str(e.get("ip", "")).strip() == ip_address for e in entries):
        return False, f"IP {ip_address} je již zabanována."
    entries.append({
        "ip": ip_address,
        "created": datetime.now().strftime("%d.%m.%y %H:%M") if storage == "legacy" else datetime.now().strftime("%Y-%m-%d %H:%M:%S +0000"),
        "source": "Server",
        "expires": "forever",
        "reason": reason,
    })
    ok = _write_legacy_ip_bans(server, entries) if storage == "legacy" else _write_json_file(server, filename, entries)
    return (True, f"IP {ip_address} zabanována.") if ok else (False, "Chyba zápisu souboru.")


def ip_ban_remove(server, ip_address: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"pardon-ip {ip_address}")
        if ok:
            return True, resp or f"IP {ip_address} odbanována."
    storage, filename = _resolve_ip_ban_file(server)
    entries = [e for e in ip_ban_list(server) if str(e.get("ip", "")).strip() != ip_address]
    ok = _write_legacy_ip_bans(server, entries) if storage == "legacy" else _write_json_file(server, filename, entries)
    return (True, f"IP {ip_address} odbanována.") if ok else (False, "Chyba zápisu souboru.")
