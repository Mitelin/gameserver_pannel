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
import calendar
from datetime import datetime, timedelta
from pathlib import Path

from django.utils import timezone

logger = logging.getLogger(__name__)

AUTO_BACKUP_INTERVAL_HOURS = 3
AUTO_BACKUP_INTERVAL = timedelta(hours=AUTO_BACKUP_INTERVAL_HOURS)
BACKUP_KIND_HOURLY = "HOURLY"
BACKUP_KIND_DAILY = "DAILY"
BACKUP_KIND_WEEKLY = "WEEKLY"
BACKUP_KIND_MONTHLY = "MONTHLY"
BACKUP_KIND_USER = "USER"
BACKUP_KIND_LEGACY = "LEGACY"
AUTO_BACKUP_KINDS = (
    BACKUP_KIND_HOURLY,
    BACKUP_KIND_DAILY,
    BACKUP_KIND_WEEKLY,
    BACKUP_KIND_MONTHLY,
)
_PANEL_BACKUP_FILENAME_RE = re.compile(
    r"^(?P<slug>.+)-(?P<timestamp>\d{8}-\d{6})(?:-(?P<kind>HOURLY|DAILY|WEEKLY|MONTHLY|USER))?\.tar\.gz$",
    re.IGNORECASE,
)


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

    backup_kind = (match.group("kind") or BACKUP_KIND_LEGACY).upper()
    is_user = backup_kind == BACKUP_KIND_USER
    return {
        "name": file_path.name,
        "path": str(file_path),
        "size_bytes": stat.st_size,
        "created_at_dt": created_at,
        "created_at": created_at.isoformat(),
        "is_user": is_user,
        "kind": "USER" if is_user else "AUTO",
        "backup_kind": backup_kind,
    }


def _list_panel_backups(path: Path, server_slug: str) -> list[dict]:
    backups = []
    for file_path in path.rglob(f"{server_slug}-*.tar.gz"):
        if not file_path.is_file():
            continue
        entry = _parse_panel_backup_entry(server_slug, file_path)
        if entry is not None:
            backups.append(entry)
    backups.sort(key=lambda item: item["created_at_dt"], reverse=True)
    return backups


def _layer_due_result(backup_kind: str, due: bool, message: str, newest: dict | None = None) -> dict:
    result = {
        "backup_kind": backup_kind,
        "due": due,
        "message": message,
    }
    if newest is not None:
        result["newest_file"] = newest["name"]
        result["last_backup"] = newest["created_at"]
    return result


def check_backup_layers_due(server, *, now=None) -> dict:
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    backup_dir = getattr(server, "backup_directory", "")

    if not backup_dir:
        return {"ok": None, "due_kinds": [], "layers": [], "message": "Backup adresář není nastaven."}

    path = Path(backup_dir)
    try:
        files = _list_panel_backups(path, server.slug) if path.exists() else []
    except PermissionError as exc:
        return {"ok": False, "due_kinds": [], "layers": [], "message": f"Přístup odepřen: {exc}"}

    newest_by_kind = {}
    for item in files:
        backup_kind = item["backup_kind"]
        if backup_kind in AUTO_BACKUP_KINDS and backup_kind not in newest_by_kind:
            newest_by_kind[backup_kind] = item

    hourly = newest_by_kind.get(BACKUP_KIND_HOURLY)
    hourly_due = hourly is None or now - hourly["created_at_dt"] >= AUTO_BACKUP_INTERVAL
    hourly_message = (
        "Nenalezena žádná HOURLY záloha."
        if hourly is None
        else f"Poslední HOURLY záloha je stará {(now - hourly['created_at_dt']).total_seconds() / 3600:.1f}h."
    )

    daily = newest_by_kind.get(BACKUP_KIND_DAILY)
    daily_due = local_now.hour >= 2 and (daily is None or timezone.localtime(daily["created_at_dt"]).date() < local_now.date())
    daily_message = "DAILY záloha se vytváří jednou denně po 02:00."

    weekly = newest_by_kind.get(BACKUP_KIND_WEEKLY)
    weekly_due = local_now.hour >= 3 and (weekly is None or local_now.date() - timezone.localtime(weekly["created_at_dt"]).date() >= timedelta(days=7))
    weekly_message = "WEEKLY záloha se vytváří po 03:00 nejdříve 7 dní od předchozí."

    monthly = newest_by_kind.get(BACKUP_KIND_MONTHLY)
    is_last_day = local_now.day == calendar.monthrange(local_now.year, local_now.month)[1]
    monthly_exists = monthly is not None and (
        timezone.localtime(monthly["created_at_dt"]).year,
        timezone.localtime(monthly["created_at_dt"]).month,
    ) == (local_now.year, local_now.month)
    monthly_due = is_last_day and local_now.hour >= 4 and not monthly_exists
    monthly_message = "MONTHLY záloha se vytváří poslední den měsíce po 04:00."

    layers = [
        _layer_due_result(BACKUP_KIND_HOURLY, hourly_due, hourly_message, hourly),
        _layer_due_result(BACKUP_KIND_DAILY, daily_due, daily_message, daily),
        _layer_due_result(BACKUP_KIND_WEEKLY, weekly_due, weekly_message, weekly),
        _layer_due_result(BACKUP_KIND_MONTHLY, monthly_due, monthly_message, monthly),
    ]
    due_kinds = [layer["backup_kind"] for layer in layers if layer["due"]]
    return {
        "ok": True,
        "due_kinds": due_kinds,
        "layers": layers,
        "message": f"Splatné vrstvy: {', '.join(due_kinds) if due_kinds else 'žádné'}.",
    }


def check_auto_backup_due(server, *, now=None) -> dict:
    result = check_backup_layers_due(server, now=now)
    hourly = next((layer for layer in result.get("layers", []) if layer["backup_kind"] == BACKUP_KIND_HOURLY), None)
    if hourly is None:
        return {"ok": result["ok"], "due": False, "message": result["message"]}
    return {"ok": result["ok"], "interval_hours": AUTO_BACKUP_INTERVAL_HOURS, **hourly}


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
