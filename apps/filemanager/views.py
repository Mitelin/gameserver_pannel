"""
apps/filemanager/views.py

File manager pro server working directory.
Bezpečnostní pravidla:
  - Veškeré cesty jsou ověřeny aby zůstaly uvnitř working_directory (anti path-traversal)
  - Pouze staff má přístup
  - Symlinky vedoucí ven z root jsou blokovány
"""
import os
import mimetypes
import logging
from pathlib import Path

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse, Http404
from django.contrib import messages
from django.views.decorators.http import require_POST

from apps.servers.models import Server

logger = logging.getLogger(__name__)

# Přípony editovatelných textových souborů
TEXT_EXTENSIONS = {
    ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".sh", ".bat", ".py", ".java", ".xml", ".html", ".css", ".js",
    ".md", ".env", ".gitignore", ".htaccess",
}
MAX_EDIT_SIZE = 512 * 1024   # 512 KB — větší soubory jen ke stažení
MAX_UPLOAD_SIZE = 64 * 1024 * 1024  # 64 MB


def _resolve_safe(root: Path, rel: str) -> Path:
    """
    Rozloží rel cestu vůči root a ověří, že výsledek je uvnitř root.
    Raises Http404 pokud dojde k path traversal.
    """
    root = root.resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)):
        logger.warning("Path traversal attempt: root=%s rel=%s", root, rel)
        raise Http404("Přístup odepřen.")
    return target


def _require_staff(request):
    if not request.user.is_staff:
        raise Http404("Pouze správci mají přístup k file manageru.")


@login_required
def path_picker_api(request):
    """
    JSON API pro procházení souborového systému v modalu pro výběr cesty.
    Vrátí seznam adresářů a souborů pro danou cestu.
    Přístupné pouze pro staff.
    """
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "message": "Přístup odepřen."}, status=403)

    raw = request.GET.get("path", "").strip()
    show_files = request.GET.get("files", "1") == "1"

    # Výchozí cesta – root souborového systému
    if not raw:
        import sys
        raw = "C:\\" if sys.platform == "win32" else "/"

    try:
        current = Path(raw).resolve()
    except Exception:
        return JsonResponse({"ok": False, "message": "Neplatná cesta."}, status=400)

    if not current.exists():
        # Pokud neexistuje, zkus parent
        current = current.parent if current.parent.exists() else Path(raw).parent.resolve()

    if not current.is_dir():
        current = current.parent

    entries = []
    try:
        for item in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                is_dir = item.is_dir()
                if not is_dir and not show_files:
                    continue
                entries.append({
                    "name":   item.name,
                    "path":   str(item),
                    "is_dir": is_dir,
                })
            except PermissionError:
                continue
    except PermissionError:
        return JsonResponse({"ok": False, "message": f"Přístup odepřen: {current}"}, status=403)

    # Sestavení breadcrumb
    parts = []
    p = current
    while True:
        parts.append({"name": p.name or str(p), "path": str(p)})
        parent = p.parent
        if parent == p:
            break
        p = parent
    parts.reverse()

    return JsonResponse({
        "ok":      True,
        "current": str(current),
        "parent":  str(current.parent) if current.parent != current else None,
        "parts":   parts,
        "entries": entries,
    })


@login_required
def file_browser(request, slug):
    """Zobrazí obsah adresáře."""
    _require_staff(request)
    server = get_object_or_404(Server, slug=slug, is_active=True)
    root   = Path(server.working_directory)

    rel    = request.GET.get("path", "").lstrip("/\\")
    try:
        current = _resolve_safe(root, rel)
    except Http404:
        messages.error(request, "Neplatná cesta.")
        return redirect(f"/servers/{slug}/files/")

    if not current.exists():
        messages.error(request, "Adresář nebo soubor neexistuje.")
        return redirect(f"/servers/{slug}/files/")

    # Pokud je to soubor, přesměruj na view/edit
    if current.is_file():
        return redirect(f"/servers/{slug}/files/view/?path={rel}")

    # Sestav seznam položek
    entries = []
    try:
        for item in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            item_rel = str(item.relative_to(root)).replace("\\", "/")
            try:
                stat = item.stat()
                size = stat.st_size
            except OSError:
                size = 0
            entries.append({
                "name":   item.name,
                "rel":    item_rel,
                "is_dir": item.is_dir(),
                "size":   size,
                "editable": (
                    item.is_file() and
                    item.suffix.lower() in TEXT_EXTENSIONS and
                    size <= MAX_EDIT_SIZE
                ),
            })
    except PermissionError:
        messages.error(request, "Nemám oprávnění číst tento adresář.")

    # Breadcrumbs
    breadcrumbs = []
    parts = Path(rel).parts if rel else []
    for i, part in enumerate(parts):
        breadcrumbs.append({
            "name": part,
            "rel":  "/".join(parts[:i+1]),
        })

    parent_rel = str(Path(rel).parent).replace("\\", "/") if rel else ""
    if parent_rel == ".":
        parent_rel = ""

    return render(request, "filemanager/browser.html", {
        "server":      server,
        "entries":     entries,
        "current_rel": rel,
        "parent_rel":  parent_rel,
        "breadcrumbs": breadcrumbs,
        "root":        str(root),
    })


@login_required
def file_view(request, slug):
    """Zobrazí nebo edituje textový soubor."""
    _require_staff(request)
    server = get_object_or_404(Server, slug=slug, is_active=True)
    root   = Path(server.working_directory)
    rel    = request.GET.get("path", "").lstrip("/\\")

    if not rel:
        return redirect(f"/servers/{slug}/files/")

    try:
        target = _resolve_safe(root, rel)
    except Http404:
        messages.error(request, "Neplatná cesta.")
        return redirect(f"/servers/{slug}/files/")

    if not target.exists() or not target.is_file():
        raise Http404("Soubor nenalezen.")

    size = target.stat().st_size
    is_text = target.suffix.lower() in TEXT_EXTENSIONS and size <= MAX_EDIT_SIZE

    error = None
    content = ""

    if request.method == "POST" and is_text:
        content = request.POST.get("content", "")
        try:
            target.write_text(content, encoding="utf-8")
            messages.success(request, f"Soubor '{target.name}' byl uložen.")
            return redirect(f"/servers/{slug}/files/view/?path={rel}")
        except OSError as e:
            error = str(e)

    if is_text:
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            error = str(e)

    dir_rel = str(Path(rel).parent).replace("\\", "/")
    if dir_rel == ".":
        dir_rel = ""

    return render(request, "filemanager/editor.html", {
        "server":  server,
        "rel":     rel,
        "dir_rel": dir_rel,
        "name":    target.name,
        "content": content,
        "is_text": is_text,
        "size":    size,
        "error":   error,
    })


@login_required
def file_download(request, slug):
    """Stáhne soubor."""
    _require_staff(request)
    server = get_object_or_404(Server, slug=slug, is_active=True)
    root   = Path(server.working_directory)
    rel    = request.GET.get("path", "").lstrip("/\\")

    try:
        target = _resolve_safe(root, rel)
    except Http404:
        raise Http404("Neplatná cesta.")

    if not target.exists() or not target.is_file():
        raise Http404("Soubor nenalezen.")

    try:
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        response = HttpResponse(target.read_bytes(), content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{target.name}"'
        return response
    except OSError as e:
        raise Http404(f"Nelze číst soubor: {e}")


@login_required
@require_POST
def file_delete(request, slug):
    """Smaže soubor (ne adresář)."""
    _require_staff(request)
    server = get_object_or_404(Server, slug=slug, is_active=True)
    root   = Path(server.working_directory)
    rel    = request.POST.get("path", "").lstrip("/\\")

    try:
        target = _resolve_safe(root, rel)
    except Http404:
        return JsonResponse({"ok": False, "message": "Neplatná cesta."}, status=400)

    if not target.exists():
        return JsonResponse({"ok": False, "message": "Soubor neexistuje."}, status=404)

    if target.is_dir():
        return JsonResponse({"ok": False, "message": "Nelze smazat adresář tímto způsobem."}, status=400)

    try:
        target.unlink()
        dir_rel = str(target.parent.relative_to(root)).replace("\\", "/")
        if dir_rel == ".":
            dir_rel = ""
        return JsonResponse({"ok": True, "message": f"'{target.name}' smazán.", "dir_rel": dir_rel})
    except OSError as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=500)


@login_required
@require_POST
def file_upload(request, slug):
    """Nahraje soubor do adresáře."""
    _require_staff(request)
    server = get_object_or_404(Server, slug=slug, is_active=True)
    root   = Path(server.working_directory)
    rel    = request.POST.get("path", "").lstrip("/\\")

    try:
        target_dir = _resolve_safe(root, rel)
    except Http404:
        return JsonResponse({"ok": False, "message": "Neplatná cesta."}, status=400)

    if not target_dir.is_dir():
        return JsonResponse({"ok": False, "message": "Cílová cesta není adresář."}, status=400)

    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"ok": False, "message": "Žádný soubor."}, status=400)

    if uploaded.size > MAX_UPLOAD_SIZE:
        return JsonResponse({"ok": False, "message": f"Soubor je příliš velký (max {MAX_UPLOAD_SIZE//1048576} MB)."}, status=400)

    # Sanitize filename
    safe_name = Path(uploaded.name).name
    if not safe_name or safe_name.startswith(".") and len(safe_name) == 1:
        return JsonResponse({"ok": False, "message": "Neplatný název souboru."}, status=400)

    dest = target_dir / safe_name
    try:
        with open(dest, "wb") as f:
            for chunk in uploaded.chunks():
                f.write(chunk)
        return JsonResponse({"ok": True, "message": f"'{safe_name}' nahrán."})
    except OSError as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=500)


@login_required
@require_POST
def mkdir(request, slug):
    """Vytvoří nový adresář."""
    _require_staff(request)
    server = get_object_or_404(Server, slug=slug, is_active=True)
    root   = Path(server.working_directory)
    rel    = request.POST.get("path", "").lstrip("/\\")
    name   = request.POST.get("name", "").strip()

    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return JsonResponse({"ok": False, "message": "Neplatný název adresáře."}, status=400)

    try:
        parent = _resolve_safe(root, rel)
        new_dir = _resolve_safe(root, (rel + "/" + name) if rel else name)
    except Http404:
        return JsonResponse({"ok": False, "message": "Neplatná cesta."}, status=400)

    try:
        new_dir.mkdir(exist_ok=True)
        return JsonResponse({"ok": True, "message": f"Adresář '{name}' vytvořen."})
    except OSError as e:
        return JsonResponse({"ok": False, "message": str(e)}, status=500)
