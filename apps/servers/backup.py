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
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from django.utils import timezone

logger = logging.getLogger(__name__)

AUTO_BACKUP_INTERVAL_HOURS = 3
AUTO_BACKUP_INTERVAL = timedelta(hours=AUTO_BACKUP_INTERVAL_HOURS)
_PANEL_BACKUP_FILENAME_RE = re.compile(r"^(?P<slug>.+)-(?P<timestamp>\d{8}-\d{6})(?P<user>-USER)?\.tar\.gz$", re.IGNORECASE)


def _parse_panel_backup_entry(server_slug: str, file_path: Path):
    match = _PANEL_BACKUP_FILENAME_RE.match(file_path.name)
    if not match or match.group("slug") != server_slug:
        return None

    try:
        created_at = timezone.make_aware(
            datetime.strptime(match.group("timestamp"), "%Y%m%d-%H%M%S"),
            timezone.get_current_timezone(),
        )
    except ValueError:
        return None

    try:
        stat = file_path.stat()
    except OSError:
        return None

    is_user = bool(match.group("user"))
    return {
        "name": file_path.name,
        "path": str(file_path),
        "size_bytes": stat.st_size,
        "created_at_dt": created_at,
        "created_at": created_at.isoformat(),
        "is_user": is_user,
        "kind": "USER" if is_user else "AUTO",
    }


def _list_panel_backups(path: Path, server_slug: str) -> list[dict]:
    backups = []
    for file_path in path.iterdir():
        if not file_path.is_file():
            continue
        entry = _parse_panel_backup_entry(server_slug, file_path)
        if entry is not None:
            backups.append(entry)
    backups.sort(key=lambda item: item["created_at_dt"], reverse=True)
    return backups


def check_auto_backup_due(server, *, now=None) -> dict:
    now = now or timezone.now()
    backup_dir = getattr(server, "backup_directory", "")

    if not backup_dir:
        return {"ok": None, "due": False, "message": "Backup adresář není nastaven."}

    path = Path(backup_dir)
    if not path.exists():
        return {
            "ok": True,
            "due": True,
            "interval_hours": AUTO_BACKUP_INTERVAL_HOURS,
            "message": "Backup adresář zatím neexistuje; vytvářím první AUTO zálohu.",
        }

    try:
        files = _list_panel_backups(path, server.slug)
    except PermissionError as exc:
        return {"ok": False, "due": False, "message": f"Přístup odepřen: {exc}"}

    if not files:
        return {
            "ok": True,
            "due": True,
            "interval_hours": AUTO_BACKUP_INTERVAL_HOURS,
            "message": "Nenalezena žádná panelová záloha; vytvářím první AUTO zálohu.",
        }

    newest = files[0]
    age = now - newest["created_at_dt"]
    age_hours = age.total_seconds() / 3600
    due = age >= AUTO_BACKUP_INTERVAL

    if due:
        message = (
            f"Poslední panelová záloha {newest['name']} ({newest['kind']}) je stará {age_hours:.1f}h; "
            f"překročen {AUTO_BACKUP_INTERVAL_HOURS}h interval, AUTO záloha je splatná."
        )
    else:
        message = (
            f"Poslední panelová záloha {newest['name']} ({newest['kind']}) je stará {age_hours:.1f}h; "
            f"AUTO záloha zatím není splatná před {AUTO_BACKUP_INTERVAL_HOURS}h intervalem."
        )

    return {
        "ok": True,
        "due": due,
        "last_backup": newest["created_at"],
        "age_hours": round(age_hours, 1),
        "interval_hours": AUTO_BACKUP_INTERVAL_HOURS,
        "newest_file": newest["name"],
        "newest_kind": newest["kind"],
        "message": message,
    }


def check_backup_status(server, *, now=None) -> dict:
    """
    Zkontroluje stav backupů pro server.
    Vrací dict s informacemi o posledním backupu.

    server musí mít atributy:
      backup_directory  (str)
      backup_max_age_hours (int)
    """
    now = now or timezone.now()
    backup_dir = getattr(server, "backup_directory", "")
    max_age_h  = getattr(server, "backup_max_age_hours", 24)

    if not backup_dir:
        return {"ok": None, "message": "Backup adresář není nastaven."}

    path = Path(backup_dir)
    if not path.exists():
        return {"ok": False, "message": f"Backup adresář neexistuje: {backup_dir}"}

    try:
        files = _list_panel_backups(path, server.slug)
    except PermissionError as exc:
        return {"ok": False, "message": f"Přístup odepřen: {exc}"}

    if not files:
        return {"ok": False, "message": "Nenalezena žádná panelová záloha."}

    newest = files[0]
    backup_time = newest["created_at_dt"]
    age_hours = (now - backup_time).total_seconds() / 3600
    size_mb = sum(item["size_bytes"] for item in files) / 1048576

    ok = age_hours <= max_age_h

    return {
        "ok":            ok,
        "last_backup":   backup_time.isoformat(),
        "age_hours":     round(age_hours, 1),
        "max_age_hours": max_age_h,
        "file_count":    len(files),
        "total_size_mb": round(size_mb, 1),
        "newest_file":   newest["name"],
        "message": (
            f"Poslední backup: {newest['name']} ({age_hours:.1f}h)"
            if ok else
            f"Backup starý {age_hours:.1f}h – limit je {max_age_h}h!"
        ),
    }
