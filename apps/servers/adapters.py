"""
apps/servers/adapters.py  (fáze 4)

Kompletní GameAdapter implementace:
  - BaseGameAdapter (ABC)
  - MinecraftJavaAdapter  – parsování logů, join/leave, TPS, Done pattern
  - MinecraftBedrockAdapter
  - TerrariaAdapter
  - FactorioAdapter
  - NullAdapter (fallback)

Každý adapter implementuje:
  startup_patterns()     – regex pro detekci úspěšného startu
  shutdown_patterns()    – regex pro detekci čistého vypnutí
  parse_console_line()   – parsování struktury řádku (level, thread, message)
  extract_player_events()– join/leave eventy
  extract_player_count() – aktuální počet hráčů z řádku logu
  extract_tps()          – TPS (jen Minecraft)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import re


@dataclass
class PlayerEvent:
    event_type:  str           # "join" | "leave"
    player_name: str
    timestamp:   Optional[str] = None


class BaseGameAdapter(ABC):

    @abstractmethod
    def startup_patterns(self) -> list[str]: ...

    @abstractmethod
    def shutdown_patterns(self) -> list[str]: ...

    @abstractmethod
    def parse_console_line(self, line: str) -> dict: ...

    @abstractmethod
    def extract_player_events(self, line: str) -> Optional[PlayerEvent]: ...

    def extract_player_count(self, line: str) -> Optional[int]:
        """Vrátí počet hráčů pokud ho řádek obsahuje, jinak None."""
        return None

    def extract_tps(self, line: str) -> Optional[float]:
        """Vrátí TPS pokud ho řádek obsahuje, jinak None."""
        return None

    def is_startup_complete(self, line: str) -> bool:
        return any(re.search(p, line) for p in self.startup_patterns())

    def is_shutdown_complete(self, line: str) -> bool:
        return any(re.search(p, line) for p in self.shutdown_patterns())

    def game_type(self) -> str:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Minecraft Java
# ─────────────────────────────────────────────────────────────────────────────

class MinecraftJavaAdapter(BaseGameAdapter):
    """
    Minecraft Java Edition (vanilla, Paper, Spigot, Forge, Fabric).

    Příklady log řádků:
      [12:34:56] [Server thread/INFO]: Done (3.456s)! For help, type "help"
      [12:34:56] [Server thread/INFO]: Steve joined the game
      [12:34:56] [Server thread/INFO]: Steve left the game
      [12:34:56] [Server thread/INFO]: There are 3 of a max of 20 players online
      [12:34:56] [Server thread/INFO]: TPS from last 1m, 5m, 15m: 19.98, 19.97, 19.95
    """

    _LOG = re.compile(
        r"^\[(?P<time>\d{2}:\d{2}:\d{2})\] "
        r"\[(?P<thread>[^\]]+)/(?P<level>[A-Z]+)\]: "
        r"(?P<message>.+)$"
    )
    _JOIN  = re.compile(r"^(\w+) joined the game$")
    _LEAVE = re.compile(r"^(\w+) left the game$")
    _PLAYERS = re.compile(r"There are (\d+) of a max of \d+ players online")
    _TPS   = re.compile(r"TPS from last 1m.*?: ([\d.]+)")

    def game_type(self): return "minecraft_java"

    def startup_patterns(self):
        return [
            r"Done \(\d+\.\d+s\)!",
            r"For help, type \"help\"",
        ]

    def shutdown_patterns(self):
        return [
            r"Stopping the server",
            r"ThreadedAnvilChunkStorage.*saved",
            r"Saving worlds",
        ]

    def parse_console_line(self, line: str) -> dict:
        m = self._LOG.match(line)
        if not m:
            return {"raw": line, "level": "unknown", "message": line}
        level = m.group("level").lower()
        return {
            "time":    m.group("time"),
            "thread":  m.group("thread"),
            "level":   level,
            "message": m.group("message"),
            "raw":     line,
        }

    def extract_player_events(self, line: str) -> Optional[PlayerEvent]:
        m = self._LOG.match(line)
        if not m:
            return None
        msg = m.group("message")
        j = self._JOIN.match(msg)
        if j:
            return PlayerEvent("join", j.group(1))
        l = self._LEAVE.match(msg)
        if l:
            return PlayerEvent("leave", l.group(1))
        return None

    def extract_player_count(self, line: str) -> Optional[int]:
        m = self._PLAYERS.search(line)
        return int(m.group(1)) if m else None

    def extract_tps(self, line: str) -> Optional[float]:
        m = self._TPS.search(line)
        return float(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Minecraft Bedrock
# ─────────────────────────────────────────────────────────────────────────────

class MinecraftBedrockAdapter(BaseGameAdapter):
    """
    Minecraft Bedrock Edition (BDS).

    Příklady:
      [2024-01-01 12:34:56 INFO] Server started.
      [2024-01-01 12:34:56 INFO] Player connected: Steve, xuid: 123456
      [2024-01-01 12:34:56 INFO] Player disconnected: Steve, xuid: 123456
    """

    _LOG     = re.compile(r"^\[[\d\- :]+(?P<level>INFO|WARN|ERROR)\] (?P<message>.+)$")
    _JOIN    = re.compile(r"Player connected: (\w+),")
    _LEAVE   = re.compile(r"Player disconnected: (\w+),")

    def game_type(self): return "minecraft_bedrock"

    def startup_patterns(self):
        return [r"Server started\.", r"IPv4 supported, port:"]

    def shutdown_patterns(self):
        return [r"Quit correctly", r"Server stop requested"]

    def parse_console_line(self, line: str) -> dict:
        m = self._LOG.match(line)
        if not m:
            return {"raw": line, "level": "unknown", "message": line}
        return {
            "level":   m.group("level").lower(),
            "message": m.group("message"),
            "raw":     line,
        }

    def extract_player_events(self, line: str) -> Optional[PlayerEvent]:
        j = self._JOIN.search(line)
        if j:
            return PlayerEvent("join", j.group(1))
        l = self._LEAVE.search(line)
        if l:
            return PlayerEvent("leave", l.group(1))
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Terraria (TShock)
# ─────────────────────────────────────────────────────────────────────────────

class TerrariaAdapter(BaseGameAdapter):
    """
    Terraria s TShock server pluginem.

    Příklady:
      [12:34:56] [Server] Server started
      [12:34:56] [Info] Steve has joined.
      [12:34:56] [Info] Steve has left.
      [12:34:56] [Info] Saving world...
    """

    _LOG   = re.compile(r"^\[[\d:]+\] \[(?P<level>[^\]]+)\] (?P<message>.+)$")
    _JOIN  = re.compile(r"^(\S+) has joined\.")
    _LEAVE = re.compile(r"^(\S+) has left\.")
    _PLAYERS = re.compile(r"Currently (\d+) players online")

    def game_type(self): return "terraria"

    def startup_patterns(self):
        return [
            r"Server started",
            r"Type 'help' for a list of commands",
            r"Listening on port",
        ]

    def shutdown_patterns(self):
        return [r"Saving world\.\.\.", r"Server shutting down"]

    def parse_console_line(self, line: str) -> dict:
        m = self._LOG.match(line)
        if not m:
            return {"raw": line, "level": "unknown", "message": line}
        level_raw = m.group("level").lower()
        level = "error" if "error" in level_raw else "warn" if "warn" in level_raw else "info"
        return {"level": level, "message": m.group("message"), "raw": line}

    def extract_player_events(self, line: str) -> Optional[PlayerEvent]:
        m = self._LOG.match(line)
        if not m:
            return None
        msg = m.group("message")
        j = self._JOIN.match(msg)
        if j:
            return PlayerEvent("join", j.group(1))
        l = self._LEAVE.match(msg)
        if l:
            return PlayerEvent("leave", l.group(1))
        return None

    def extract_player_count(self, line: str) -> Optional[int]:
        m = self._PLAYERS.search(line)
        return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Factorio
# ─────────────────────────────────────────────────────────────────────────────

class FactorioAdapter(BaseGameAdapter):
    """
    Factorio dedicated server.

    Příklady:
       0.000 2024-01-01 12:34:56; Factorio 1.1.99 (build 60050)
       4.123 Info ServerMultiplayerManager.cpp: updateTick: Steve joined the game
       4.456 Info ServerMultiplayerManager.cpp: updateTick: Steve left the game
      15.000 Info ServerMultiplayerManager.cpp: Saving game as...
    """

    _LOG   = re.compile(r"^\s*[\d.]+ (?P<level>Info|Warning|Error) (?P<source>\S+): (?P<message>.+)$")
    _JOIN  = re.compile(r"(\S+) joined the game")
    _LEAVE = re.compile(r"(\S+) left the game")

    def game_type(self): return "factorio"

    def startup_patterns(self):
        return [
            r"changing state from\(CreatingGame\) to\(InGame\)",
            r"Hosting game at",
        ]

    def shutdown_patterns(self):
        return [
            r"changing state from\(InGame\) to\(Disconnected\)",
            r"Server quitting",
        ]

    def parse_console_line(self, line: str) -> dict:
        m = self._LOG.match(line)
        if not m:
            return {"raw": line, "level": "unknown", "message": line}
        level_raw = m.group("level").lower()
        level = "error" if level_raw == "error" else "warn" if level_raw == "warning" else "info"
        return {
            "level":   level,
            "source":  m.group("source"),
            "message": m.group("message"),
            "raw":     line,
        }

    def extract_player_events(self, line: str) -> Optional[PlayerEvent]:
        j = self._JOIN.search(line)
        if j:
            return PlayerEvent("join", j.group(1))
        l = self._LEAVE.search(line)
        if l:
            return PlayerEvent("leave", l.group(1))
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Null adapter (fallback)
# ─────────────────────────────────────────────────────────────────────────────

class NullAdapter(BaseGameAdapter):
    """Fallback pro neznámý game_type – vše projde, nic se neparsuje."""
    def game_type(self): return "other"
    def startup_patterns(self): return []
    def shutdown_patterns(self): return []
    def parse_console_line(self, line: str): return {"raw": line, "level": "unknown", "message": line}
    def extract_player_events(self, line: str): return None


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[BaseGameAdapter]] = {
    "minecraft_java":    MinecraftJavaAdapter,
    "minecraft_bedrock": MinecraftBedrockAdapter,
    "terraria":          TerrariaAdapter,
    "factorio":          FactorioAdapter,
}


def get_adapter(game_type: str) -> BaseGameAdapter:
    cls = _REGISTRY.get(game_type)
    return cls() if cls else NullAdapter()


def register_adapter(game_type: str, cls: type[BaseGameAdapter]):
    """Umožňuje registraci custom adapterů z externích Django appek."""
    _REGISTRY[game_type] = cls
