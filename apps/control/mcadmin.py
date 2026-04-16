"""
apps/control/mcadmin.py

Minecraft whitelist/ban správa přes RCON nebo přímou editaci JSON souborů.

Preferuje RCON pokud je povoleno; fallback na soubory whitelist.json / banned-players.json
v server.working_directory.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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


def _read_json_file(server, filename: str) -> list[dict]:
    path = Path(server.working_directory) / filename
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Nelze číst %s: %s", filename, exc)
        return []


def _write_json_file(server, filename: str, data: list):
    path = Path(server.working_directory) / filename
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as exc:
        logger.error("Nelze zapsat %s: %s", filename, exc)
        return False


# ── Whitelist ────────────────────────────────────────────────────────────────

def whitelist_list(server) -> list[dict]:
    return _read_json_file(server, "whitelist.json")


def whitelist_add(server, player_name: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"whitelist add {player_name}")
        if ok:
            return True, resp or f"{player_name} přidán na whitelist."
    # Fallback – přímá editace souboru
    entries = whitelist_list(server)
    if any(e.get("name", "").lower() == player_name.lower() for e in entries):
        return False, f"{player_name} je již na whitelistu."
    entries.append({"uuid": "", "name": player_name})
    ok = _write_json_file(server, "whitelist.json", entries)
    return (True, f"{player_name} přidán.") if ok else (False, "Chyba zápisu souboru.")


def whitelist_remove(server, player_name: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"whitelist remove {player_name}")
        if ok:
            return True, resp or f"{player_name} odebrán z whitelistu."
    entries = [e for e in whitelist_list(server) if e.get("name", "").lower() != player_name.lower()]
    ok = _write_json_file(server, "whitelist.json", entries)
    return (True, f"{player_name} odebrán.") if ok else (False, "Chyba zápisu souboru.")


# ── Ban list ─────────────────────────────────────────────────────────────────

def ban_list(server) -> list[dict]:
    return _read_json_file(server, "banned-players.json")


def ban_add(server, player_name: str, reason: str = "Banned by admin") -> tuple[bool, str]:
    if server.rcon_enabled:
        cmd = f"ban {player_name} {reason}" if reason else f"ban {player_name}"
        ok, resp = _rcon_command(server, cmd)
        if ok:
            return True, resp or f"{player_name} zabanován."
    entries = ban_list(server)
    if any(e.get("name", "").lower() == player_name.lower() for e in entries):
        return False, f"{player_name} je již zabanován."
    from datetime import datetime
    entries.append({
        "uuid":    "",
        "name":    player_name,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S +0000"),
        "source":  "Server",
        "expires": "forever",
        "reason":  reason,
    })
    ok = _write_json_file(server, "banned-players.json", entries)
    return (True, f"{player_name} zabanován.") if ok else (False, "Chyba zápisu souboru.")


def ban_remove(server, player_name: str) -> tuple[bool, str]:
    if server.rcon_enabled:
        ok, resp = _rcon_command(server, f"pardon {player_name}")
        if ok:
            return True, resp or f"{player_name} odbanován."
    entries = [e for e in ban_list(server) if e.get("name", "").lower() != player_name.lower()]
    ok = _write_json_file(server, "banned-players.json", entries)
    return (True, f"{player_name} odbanován.") if ok else (False, "Chyba zápisu souboru.")
