"""
apps/servers/backup_engine.py

Vytváří reálné zálohy working_directory serveru jako tar.gz archiv.

Funkce:
    - create_backup(server)  – zazipuje working_directory → backup_directory/slug-YYYYMMDD-HHMMSS[-USER].tar.gz
    - rotate_backups(server) – aplikuje pevnou 4úrovňovou rotaci
    - list_backups(server)   – vrátí seznam existujících záloh s metadaty
"""
import tarfile
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import calendar
from os import stat_result

from django.utils import timezone

from .models import normalize_backup_exclude_paths

logger = logging.getLogger(__name__)

INTRADAY_KEEP_SLOTS = 8
INTRADAY_SLOT_HOURS = 3
DAILY_KEEP_DAYS = 7
WEEKLY_KEEP_WEEKS = 4
MONTHLY_KEEP_MONTHS = 12
USER_BACKUP_SUFFIX = "USER"
LEGACY_FILENAME_MTIME_DRIFT = timedelta(minutes=90)
LEGACY_FILENAME_OFFSET_TOLERANCE = timedelta(minutes=30)


def _current_backup_timestamp() -> str:
    return timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M%S")


def _is_backup_archive_for_server(path: Path, server_slug: str) -> bool:
    return path.is_file() and path.name.startswith(f"{server_slug}-") and path.name.endswith(".tar.gz")


def _build_tar_filter(src_dir: Path, backup_dir: Path, dest: Path, server_slug: str, excluded_relative_paths: list[str] | None = None):
    src_root = src_dir.resolve()
    backup_root = backup_dir.resolve()
    dest_path = dest.resolve()
    backup_inside_source = backup_root != src_root and backup_root.is_relative_to(src_root)
    backup_equals_source = backup_root == src_root
    excluded_roots = [
        (src_root / Path(relative_path)).resolve(strict=False)
        for relative_path in (excluded_relative_paths or [])
    ]

    def _filter(tarinfo: tarfile.TarInfo):
        relative = Path(*Path(tarinfo.name).parts[1:]) if len(Path(tarinfo.name).parts) > 1 else Path()
        absolute = (src_root / relative).resolve()

        if absolute == dest_path:
            return None
        if backup_inside_source and absolute.is_relative_to(backup_root):
            return None
        if backup_equals_source and _is_backup_archive_for_server(absolute, server_slug):
            return None
        for excluded_root in excluded_roots:
            if absolute == excluded_root or absolute.is_relative_to(excluded_root):
                return None
        return tarinfo

    return _filter


def _archive_server_files(src_dir: Path, backup_dir: Path, dest: Path, server_slug: str, excluded_relative_paths: list[str] | None = None):
    tar_filter = _build_tar_filter(src_dir, backup_dir, dest, server_slug, excluded_relative_paths)
    with tarfile.open(dest, "w:gz", compresslevel=6) as tar:
        tar.add(str(src_dir), arcname=server_slug, filter=tar_filter)


def _parse_backup_timestamp(server_slug: str, file_name: str):
    prefix = f"{server_slug}-"
    suffix = ".tar.gz"
    if not file_name.startswith(prefix) or not file_name.endswith(suffix):
        return None

    raw = file_name[len(prefix):-len(suffix)]
    parts = raw.split("-")
    if len(parts) < 2:
        return None

    ts = "-".join(parts[:2])
    try:
        parsed = datetime.strptime(ts, "%Y%m%d-%H%M%S")
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    except ValueError:
        return None


def _resolve_backup_timestamp(server_slug: str, file_name: str, file_stat: stat_result):
    parsed_dt = _parse_backup_timestamp(server_slug, file_name)
    mtime_dt = datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.get_current_timezone())
    if parsed_dt is None:
        return mtime_dt, "mtime"

    # Legacy archives created before the local-time filename fix can have a filename
    # that is about two hours behind the archive's actual creation time on disk.
    local_utc_offset = mtime_dt.utcoffset() or timedelta(0)
    mtime_delta = mtime_dt - parsed_dt
    if abs(mtime_delta) > LEGACY_FILENAME_MTIME_DRIFT and abs(mtime_delta - local_utc_offset) <= LEGACY_FILENAME_OFFSET_TOLERANCE:
        return mtime_dt, "mtime_legacy"

    return parsed_dt, "filename"


def _is_user_backup(file_name: str) -> bool:
    return file_name.upper().endswith(f"-{USER_BACKUP_SUFFIX}.TAR.GZ")


def _retention_label(bucket: str, is_kept: bool, is_user: bool) -> str:
    if is_user:
        return "USER"
    if not is_kept:
        return "Mimo rotaci"
    return {
        "intraday": "Denní 3h",
        "daily": "Denní",
        "weekly": "Týdenní",
        "monthly": "Měsíční",
    }.get(bucket, "Mimo rotaci")


def _mark_bucket(item: dict, bucket: str, marked_names: set[str], summary: dict) -> bool:
    if item["name"] in marked_names:
        return False
    marked_names.add(item["name"])
    item["retention_bucket"] = bucket
    summary["kept_total"] += 1
    summary[f"kept_{bucket}"] += 1
    return True


def _annotate_backups(backups: list[dict]) -> tuple[list[dict], dict]:
    annotated = [dict(backup) for backup in backups]
    for item in annotated:
        item["retention_bucket"] = None
        item["protected_by_rotation"] = False

    newest_auto_dt = next((item["created_at_dt"] for item in annotated if not item.get("is_user")), None)
    summary = {
        "kept_total": 0,
        "kept_user": 0,
        "kept_intraday": 0,
        "kept_daily": 0,
        "kept_weekly": 0,
        "kept_monthly": 0,
    }

    if newest_auto_dt is None:
        for item in annotated:
            item["retention_bucket"] = "user" if item.get("is_user") else None
            item["retention_label"] = _retention_label("user", True, item.get("is_user", False))
            item["protected_by_rotation"] = bool(item.get("is_user"))
            if item.get("is_user"):
                summary["kept_total"] += 1
                summary["kept_user"] += 1
        return annotated, summary

    newest_date = newest_auto_dt.date()
    marked_names: set[str] = set()
    intraday_slots: set[tuple[str, int]] = set()
    daily_buckets: set[str] = set()
    weekly_buckets: set[int] = set()
    monthly_buckets: set[tuple[int, int]] = set()

    for item in annotated:
        dt = item["created_at_dt"]
        is_user = item.get("is_user", False)

        if is_user:
            _mark_bucket(item, "user", marked_names, summary)
            continue

        day_age = (newest_date - dt.date()).days
        bucket = None

        if 0 <= (newest_auto_dt - dt).total_seconds() <= INTRADAY_KEEP_SLOTS * INTRADAY_SLOT_HOURS * 3600:
            slot_key = (dt.date().isoformat(), dt.hour // INTRADAY_SLOT_HOURS)
            if slot_key not in intraday_slots and len(intraday_slots) < INTRADAY_KEEP_SLOTS:
                intraday_slots.add(slot_key)
                bucket = "intraday"

        if bucket is None and 1 <= day_age <= DAILY_KEEP_DAYS:
            day_key = dt.date().isoformat()
            if day_key not in daily_buckets and len(daily_buckets) < DAILY_KEEP_DAYS:
                daily_buckets.add(day_key)
                bucket = "daily"

        if bucket is None and DAILY_KEEP_DAYS < day_age <= DAILY_KEEP_DAYS + (WEEKLY_KEEP_WEEKS * 7):
            week_index = (day_age - DAILY_KEEP_DAYS - 1) // 7
            if week_index not in weekly_buckets and len(weekly_buckets) < WEEKLY_KEEP_WEEKS:
                weekly_buckets.add(week_index)
                bucket = "weekly"

        if bucket is None:
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            month_key = (dt.year, dt.month)
            if dt.day == last_day and month_key not in monthly_buckets and len(monthly_buckets) < MONTHLY_KEEP_MONTHS:
                monthly_buckets.add(month_key)
                bucket = "monthly"

        if bucket is not None:
            _mark_bucket(item, bucket, marked_names, summary)

    for item in annotated:
        bucket = item.get("retention_bucket")
        item["protected_by_rotation"] = bucket is not None
        item["retention_label"] = _retention_label(bucket or "", bucket is not None, item.get("is_user", False))

    return annotated, summary

# Globální lock – zabrání souběžným záloháním stejného serveru
_backup_locks: dict[str, threading.Lock] = {}
_lock_registry = threading.Lock()


def _get_lock(slug: str) -> threading.Lock:
    with _lock_registry:
        if slug not in _backup_locks:
            _backup_locks[slug] = threading.Lock()
        return _backup_locks[slug]


def create_backup(server, *, is_user: bool = False) -> dict:
    """
    Vytvoří zálohu working_directory jako tar.gz.
    Vrátí dict: {"ok": bool, "message": str, "path": str, "size_bytes": int}
    """
    lock = _get_lock(server.slug)
    if not lock.acquire(blocking=False):
        return {"ok": False, "message": "Záloha pro tento server již probíhá."}

    try:
        return _do_backup(server, is_user=is_user)
    finally:
        lock.release()


def _do_backup(server, *, is_user: bool = False) -> dict:
    src_dir    = Path(server.working_directory)
    backup_dir = Path(server.backup_directory) if server.backup_directory else None

    if not src_dir.exists():
        return {"ok": False, "message": f"Working directory neexistuje: {src_dir}"}

    if not backup_dir:
        return {"ok": False, "message": "Backup adresář není nastaven v konfiguraci serveru."}

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "message": f"Nelze vytvořit backup adresář: {e}"}

    ts = _current_backup_timestamp()
    suffix = f"-{USER_BACKUP_SUFFIX}" if is_user else ""
    filename = f"{server.slug}-{ts}{suffix}.tar.gz"
    dest     = backup_dir / filename

    try:
        excluded_relative_paths = normalize_backup_exclude_paths(str(src_dir), getattr(server, "backup_exclude_paths", ""))
    except ValueError as exc:
        logger.error("[backup] Neplatná konfigurace vyloučených cest pro %s: %s", server.slug, exc)
        return {"ok": False, "message": f"Neplatná konfigurace vyloučených cest: {exc}"}

    logger.info("[backup] Spouštím zálohu %s → %s", src_dir, dest)
    logger.info(
        "[backup] Vyloučené relativní cesty pro %s: %s",
        server.slug,
        ", ".join(excluded_relative_paths) if excluded_relative_paths else "žádné",
    )

    try:
        _archive_server_files(src_dir, backup_dir, dest, server.slug, excluded_relative_paths)
        size = dest.stat().st_size
        logger.info("[backup] Záloha dokončena: %s (%.1f MB)", filename, size / 1048576)

        # Rotace
        rotation_result = rotate_backups(server)

        from apps.audit.models import AuditEvent
        event_type = "server.backup.user_created" if is_user else "server.backup.created"
        kind_label = "USER" if is_user else "AUTO"
        AuditEvent.objects.create(
            server=server,
            event_type=event_type,
            severity="info",
            message=f"{kind_label} záloha vytvořena: {filename} ({size/1048576:.1f} MB)",
            payload_json={"file": filename, "size_bytes": size, "is_user": is_user, **rotation_result},
        )

        return {
            "ok":         True,
            "message":    f"{kind_label} záloha '{filename}' vytvořena ({size/1048576:.1f} MB).",
            "path":       str(dest),
            "filename":   filename,
            "size_bytes": size,
            "is_user":    is_user,
        }

    except Exception as exc:
        logger.error("[backup] Záloha selhala pro %s: %s", server.slug, exc)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return {"ok": False, "message": f"Záloha selhala: {exc}"}


def rotate_backups(server) -> dict:
    """
    Aplikuje pevnou 4úrovňovou rotaci:
      - 8 intradenních slotů po 3 hodinách za posledních 24h
      - 7 denních záloh za posledních 7 dnů
      - 4 týdenní zálohy po 7 dnech
      - 12 měsíčních záloh z posledního dne měsíce
    """
    backups = list_backups(server)
    if not backups:
        return {
            "rotated": 0,
            "kept_total": 0,
            "kept_user": 0,
            "kept_intraday": 0,
            "kept_daily": 0,
            "kept_weekly": 0,
            "kept_monthly": 0,
            "kept_files": [],
            "deleted_files": [],
        }

    annotated, kept_counts = _annotate_backups(backups)
    kept_backups = [backup for backup in annotated if backup["protected_by_rotation"]]
    to_delete = [backup for backup in annotated if not backup["protected_by_rotation"]]
    kept_files = [backup["name"] for backup in kept_backups]
    deleted_files = [backup["name"] for backup in to_delete]

    logger.info(
        "[backup] Rotace summary %s: total=%d keep=%d delete=%d buckets=user:%d intraday:%d daily:%d weekly:%d monthly:%d",
        server.slug,
        len(annotated),
        kept_counts["kept_total"],
        len(to_delete),
        kept_counts["kept_user"],
        kept_counts["kept_intraday"],
        kept_counts["kept_daily"],
        kept_counts["kept_weekly"],
        kept_counts["kept_monthly"],
    )
    if kept_backups:
        logger.info(
            "[backup] Rotace keep %s: %s",
            server.slug,
            ", ".join(f"{backup['name']}[{backup['retention_bucket']}]" for backup in kept_backups),
        )

    deleted = 0
    for b in to_delete:
        try:
            Path(b["path"]).unlink()
            deleted += 1
            logger.info("[backup] Rotace: smazán %s", b["name"])
        except OSError as e:
            logger.warning("[backup] Nelze smazat %s: %s", b["name"], e)

    return {
        "rotated": deleted,
        "kept_total": kept_counts["kept_total"],
        "kept_user": kept_counts["kept_user"],
        "kept_intraday": kept_counts["kept_intraday"],
        "kept_daily": kept_counts["kept_daily"],
        "kept_weekly": kept_counts["kept_weekly"],
        "kept_monthly": kept_counts["kept_monthly"],
        "kept_files": kept_files,
        "deleted_files": deleted_files,
    }


def list_backups(server) -> list[dict]:
    """
    Vrátí seznam záloh seřazených od nejnovější.
    Každý záznam: {"name", "path", "size_bytes", "created_at", "created_at_dt", "kind", ...}
    """
    backup_dir = Path(server.backup_directory) if server.backup_directory else None
    if not backup_dir or not backup_dir.exists():
        return []

    pattern = f"{server.slug}-*.tar.gz"
    files = sorted(backup_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        try:
            st = f.stat()
            created_at_dt, timestamp_source = _resolve_backup_timestamp(server.slug, f.name, st)
            is_user = _is_user_backup(f.name)
            result.append({
                "name":       f.name,
                "path":       str(f),
                "size_bytes": st.st_size,
                "created_at": created_at_dt.strftime("%d.%m.%Y %H:%M"),
                "created_at_dt": created_at_dt,
                "is_user":    is_user,
                "kind":       "USER" if is_user else "AUTO",
                "timestamp_source": timestamp_source,
            })
        except OSError:
            pass

    result.sort(key=lambda item: item["created_at_dt"], reverse=True)
    annotated, _summary = _annotate_backups(result)
    return annotated
