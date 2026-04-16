"""
apps/servers/backup_engine.py

Vytváří reálné zálohy working_directory serveru jako tar.gz archiv.

Funkce:
  - create_backup(server)  – zazipuje working_directory → backup_directory/slug-YYYYMMDD-HHMMSS.tar.gz
  - rotate_backups(server) – smaže staré zálohy pokud je jich víc než backup_keep_count
  - list_backups(server)   – vrátí seznam existujících záloh s metadaty
"""
import tarfile
import logging
import threading
from datetime import datetime
from pathlib import Path

from django.utils import timezone

logger = logging.getLogger(__name__)

# Globální lock – zabrání souběžným záloháním stejného serveru
_backup_locks: dict[str, threading.Lock] = {}
_lock_registry = threading.Lock()


def _get_lock(slug: str) -> threading.Lock:
    with _lock_registry:
        if slug not in _backup_locks:
            _backup_locks[slug] = threading.Lock()
        return _backup_locks[slug]


def create_backup(server) -> dict:
    """
    Vytvoří zálohu working_directory jako tar.gz.
    Vrátí dict: {"ok": bool, "message": str, "path": str, "size_bytes": int}
    """
    lock = _get_lock(server.slug)
    if not lock.acquire(blocking=False):
        return {"ok": False, "message": "Záloha pro tento server již probíhá."}

    try:
        return _do_backup(server)
    finally:
        lock.release()


def _do_backup(server) -> dict:
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

    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{server.slug}-{ts}.tar.gz"
    dest     = backup_dir / filename

    logger.info("[backup] Spouštím zálohu %s → %s", src_dir, dest)

    try:
        with tarfile.open(dest, "w:gz", compresslevel=6) as tar:
            tar.add(str(src_dir), arcname=server.slug)
        size = dest.stat().st_size
        logger.info("[backup] Záloha dokončena: %s (%.1f MB)", filename, size / 1048576)

        # Rotace
        rotation_result = rotate_backups(server)

        from apps.audit.models import AuditEvent
        AuditEvent.objects.create(
            server=server,
            event_type="server.backup.created",
            severity="info",
            message=f"Záloha vytvořena: {filename} ({size/1048576:.1f} MB)",
            payload_json={"file": filename, "size_bytes": size, **rotation_result},
        )

        return {
            "ok":         True,
            "message":    f"Záloha '{filename}' vytvořena ({size/1048576:.1f} MB).",
            "path":       str(dest),
            "filename":   filename,
            "size_bytes": size,
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
    Smaže nejstarší zálohy pokud je jich více než backup_keep_count.
    Vrátí dict s počtem smazaných souborů.
    """
    keep = getattr(server, "backup_keep_count", 7)
    backups = list_backups(server)

    if len(backups) <= keep:
        return {"rotated": 0}

    to_delete = backups[keep:]   # list_backups je seřazený od nejnovějšího
    deleted = 0
    for b in to_delete:
        try:
            Path(b["path"]).unlink()
            deleted += 1
            logger.info("[backup] Rotace: smazán %s", b["name"])
        except OSError as e:
            logger.warning("[backup] Nelze smazat %s: %s", b["name"], e)

    return {"rotated": deleted, "kept": keep}


def list_backups(server) -> list[dict]:
    """
    Vrátí seznam záloh seřazených od nejnovější.
    Každý záznam: {"name", "path", "size_bytes", "created_at"}
    """
    backup_dir = Path(server.backup_directory) if server.backup_directory else None
    if not backup_dir or not backup_dir.exists():
        return []

    pattern = f"{server.slug}-*.tar.gz"
    files = sorted(
        backup_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    result = []
    for f in files:
        try:
            st = f.stat()
            result.append({
                "name":       f.name,
                "path":       str(f),
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M"),
            })
        except OSError:
            pass
    return result
