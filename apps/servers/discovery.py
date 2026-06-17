"""
apps/servers/discovery.py

Lehke autodiscovery konfigurace serveru z existujiciho working_directory.
Nevytvari DB zaznamy, jen vraci navrh poli pro formular.
"""
import sys
from pathlib import Path

from django.utils.text import slugify

from apps.servers.models import GameType


def read_properties_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def _quote_if_needed(value: str) -> str:
    return f'"{value}"' if " " in value and not value.startswith('"') else value


def _detect_minecraft_java(root: Path) -> dict | None:
    jars = sorted(root.glob("*.jar"))
    props_path = root / "server.properties"
    props = read_properties_file(props_path)
    if not jars and not props_path.exists():
        return None

    preferred = [
        "server.jar", "paper.jar", "purpur.jar", "spigot.jar", "fabric-server-launch.jar",
    ]
    jar = _first_existing([root / name for name in preferred])
    if jar is None and jars:
        ranked = sorted(
            jars,
            key=lambda item: (
                0 if any(token in item.name.lower() for token in ["paper", "purpur", "spigot", "fabric", "forge", "server"]) else 1,
                len(item.name),
            ),
        )
        jar = ranked[0]

    log_path = root / "logs" / "latest.log"
    start_command = f"java -jar {_quote_if_needed(jar.name)} nogui" if jar else ""
    return {
        "game_type": GameType.MINECRAFT_JAVA,
        "start_command": start_command,
        "stop_command": "stop",
        "log_file_path": str(log_path),
        "rcon_enabled": props.get("enable-rcon", "false").lower() == "true" or bool(props.get("rcon.password")),
        "rcon_host": "127.0.0.1",
        "rcon_port": int(props.get("rcon.port", "25575") or "25575"),
        "rcon_password": props.get("rcon.password", ""),
        "warnings": [] if jar else ["Nebyl nalezen JAR soubor. Doplň start_command ručně."],
    }


def _detect_minecraft_bedrock(root: Path) -> dict | None:
    exe_name = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"
    exe = _first_existing([root / exe_name, root / "bedrock_server.exe", root / "bedrock_server"])
    if exe is None:
        return None

    start_command = exe.name if exe.parent == root else str(exe)
    return {
        "game_type": GameType.MINECRAFT_BEDROCK,
        "start_command": _quote_if_needed(start_command),
        "stop_command": "stop",
        "log_file_path": str(root / "bedrock_server.log"),
        "warnings": [],
    }


def _detect_terraria(root: Path) -> dict | None:
    exe_candidates = [
        root / "TerrariaServer.exe",
        root / "TerrariaServer.bin.x86_64",
        root / "TShock.Server.exe",
    ]
    exe = _first_existing(exe_candidates)
    if exe is None:
        return None

    start_command = exe.name if exe.parent == root else str(exe)
    log_candidate = _first_existing([root / "server.log", root / "logs" / "server.log"])
    return {
        "game_type": GameType.TERRARIA,
        "start_command": _quote_if_needed(start_command),
        "stop_command": "exit",
        "log_file_path": str(log_candidate or (root / "server.log")),
        "warnings": [],
    }


def _detect_factorio(root: Path) -> dict | None:
    exe_candidates = [
        root / "bin" / "x64" / "factorio.exe",
        root / "bin" / "x64" / "factorio",
        root / "factorio.exe",
        root / "factorio",
    ]
    exe = _first_existing(exe_candidates)
    log_candidate = _first_existing([root / "factorio-current.log", root / "logs" / "factorio-current.log"])
    if exe is None and log_candidate is None:
        return None

    if exe is not None:
        start_target = exe.name if exe.parent == root else str(exe)
        start_command = f"{_quote_if_needed(start_target)} --start-server-load-latest"
    else:
        start_command = ""

    warnings = [] if exe is not None else ["Nebyl nalezen Factorio executable. Doplň start_command ručně."]
    return {
        "game_type": GameType.FACTORIO,
        "start_command": start_command,
        "stop_command": "stop",
        "log_file_path": str(log_candidate or (root / "factorio-current.log")),
        "warnings": warnings,
    }


def discover_server_config(directory: str) -> dict:
    root = Path(directory).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return {"ok": False, "message": f"Adresář neexistuje: {root}"}

    detectors = [
        _detect_minecraft_java,
        _detect_minecraft_bedrock,
        _detect_terraria,
        _detect_factorio,
    ]

    detected = None
    for detector in detectors:
        detected = detector(root)
        if detected:
            break

    slug = slugify(root.name) or "server"
    suggestions = {
        "name": root.name.replace("-", " ").replace("_", " ").strip() or "Server",
        "slug": slug,
        "working_directory": str(root),
        "tmux_session_name": slug.replace("-", "_")[:64],
    }

    warnings = []
    if detected:
        warnings.extend(detected.pop("warnings", []))
        suggestions.update(detected)
    else:
        suggestions.update({
            "game_type": GameType.OTHER,
            "start_command": "",
            "stop_command": "stop",
            "log_file_path": str(root / "panel_output.log"),
        })
        warnings.append("Nepodařilo se jednoznačně rozpoznat typ serveru. Zkontroluj doplněná pole ručně.")

    return {
        "ok": True,
        "message": "Konfigurace byla navržena z pracovního adresáře.",
        "suggestions": suggestions,
        "warnings": warnings,
    }