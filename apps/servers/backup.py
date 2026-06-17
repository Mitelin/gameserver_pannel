"""
apps/servers/backup.py

Backup status tracker.

Jednoduchý systém – nekopíruje soubory, pouze sleduje:
  - existenci backup adresáře
  - stáří posledního backupu
  - velikost

Nastavení se přidá do Server modelu jako backup_directory a backup_max_age_hours.
Checker se volá z watchdogu nebo jako samostatný management command.
"""
import os
import logging
from pathlib import Path

from django.utils import timezone

logger = logging.getLogger(__name__)


def check_backup_status(server) -> dict:
    """
    Zkontroluje stav backupů pro server.
    Vrací dict s informacemi o posledním backupu.

    server musí mít atributy:
      backup_directory  (str)
      backup_max_age_hours (int)
    """
    backup_dir = getattr(server, "backup_directory", "")
    max_age_h  = getattr(server, "backup_max_age_hours", 24)

    if not backup_dir:
        return {"ok": None, "message": "Backup adresář není nastaven."}

    path = Path(backup_dir)
    if not path.exists():
        return {"ok": False, "message": f"Backup adresář neexistuje: {backup_dir}"}

    pattern = f"{server.slug}-*.tar.gz"

    # Najdi nejnovější panelovou zálohu v adresáři.
    try:
        files = sorted(
            (f for f in path.glob(pattern) if f.is_file()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except PermissionError as exc:
        return {"ok": False, "message": f"Přístup odepřen: {exc}"}

    if not files:
        return {"ok": False, "message": "Nenalezena žádná panelová záloha."}

    newest      = files[0]
    mtime       = newest.stat().st_mtime
    backup_time = timezone.datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_hours   = (timezone.now() - backup_time).total_seconds() / 3600
    size_mb     = sum(f.stat().st_size for f in files) / 1048576

    ok = age_hours <= max_age_h

    return {
        "ok":            ok,
        "last_backup":   backup_time.isoformat(),
        "age_hours":     round(age_hours, 1),
        "max_age_hours": max_age_h,
        "file_count":    len(files),
        "total_size_mb": round(size_mb, 1),
        "newest_file":   str(newest.name),
        "message": (
            f"Poslední backup: {newest.name} ({age_hours:.1f}h)"
            if ok else
            f"Backup starý {age_hours:.1f}h – limit je {max_age_h}h!"
        ),
    }
