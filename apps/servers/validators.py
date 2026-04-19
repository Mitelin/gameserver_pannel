"""
apps/servers/validators.py

Validace konfigurace serveru před spuštěním akcí.
Voláno z control/service.py před start_server().
"""
import os
import shutil
import logging
from dataclasses import dataclass, field

from apps.servers.models import Server

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    ok: bool = True
    errors: list[str]   = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str):
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)


def validate_server_config(server: Server) -> ValidationResult:
    """
    Zkontroluje konfiguraci serveru před spuštěním.
    Vrací ValidationResult s chybami a varováními.
    """
    result = ValidationResult()

    # Pracovní adresář musí existovat
    if not os.path.isdir(server.working_directory):
        result.fail(
            f"Pracovní adresář neexistuje: {server.working_directory}"
        )

    # Start command nesmí být prázdný
    if not server.start_command.strip():
        result.fail("Start command je prázdný.")

    # tmux musí být dostupný (na Windows jen varování – produkce běží na Linuxu)
    if not shutil.which("tmux"):
        import sys
        if sys.platform == "win32":
            result.warn("tmux není dostupný (Windows – v produkci na Linuxu bude OK).")
        else:
            result.fail("tmux není dostupný na PATH.")

    # Log soubor nemusí existovat před startem, ale adresář musí
    if server.log_file_path:
        log_dir = os.path.dirname(server.log_file_path)
        if log_dir and not os.path.isdir(log_dir):
            result.warn(
                f"Adresář log souboru neexistuje: {log_dir} "
                f"(bude vytvořen serverem při startu)"
            )

    # PID soubor – kontrola adresáře
    if server.pid_file_path:
        pid_dir = os.path.dirname(server.pid_file_path)
        if pid_dir and not os.path.isdir(pid_dir):
            result.warn(f"Adresář PID souboru neexistuje: {pid_dir}")

    # RCON validace pokud je zapnuté
    if server.rcon_enabled:
        if not server.rcon_host:
            result.warn("RCON je zapnuté ale rcon_host je prázdný.")
        if not server.rcon_port:
            result.warn("RCON je zapnuté ale rcon_port není nastaven.")
        if not server.rcon_password:
            result.warn("RCON je zapnuté ale rcon_password je prázdný.")

    # Varování na velmi krátké timeouty
    if server.expected_startup_seconds < 10:
        result.warn(f"expected_startup_seconds={server.expected_startup_seconds} je velmi nízké.")
    if server.expected_shutdown_seconds < 5:
        result.warn(f"expected_shutdown_seconds={server.expected_shutdown_seconds} je velmi nízké.")

    if result.errors:
        logger.error("Validace serveru %s selhala: %s", server.slug, result.errors)
    if result.warnings:
        logger.warning("Validace serveru %s varování: %s", server.slug, result.warnings)

    return result
