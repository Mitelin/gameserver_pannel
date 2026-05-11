"""
apps/servers/java_detect.py

Detekuje dostupné Java instalace na Windows i Linuxu.
"""
import os
import sys
import shutil
import subprocess
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class JavaInstall:
    path: str           # absolutní cesta k java.exe / java
    version: str        # např. "21.0.3", "17.0.11", "25"
    vendor: str         # např. "Oracle", "Eclipse Temurin", "Microsoft"
    major: int          # hlavní verze (21, 17, 25 …)
    source: str         # "PATH", "registry", "scan", "JAVA_HOME"

    def label(self) -> str:
        return f"Java {self.major} – {self.vendor} ({self.source})"


def _run(cmd: list[str], timeout: int = 4) -> Optional[str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
        return (r.stdout + r.stderr).strip()
    except Exception:
        return None


def _parse_version(output: str) -> tuple[str, str, int]:
    """Vrátí (version_str, vendor, major) z výstupu 'java -version'."""
    version = "?"
    vendor  = "Unknown"
    major   = 0

    for line in output.splitlines():
        low = line.lower()

        # version string
        if "version" in low and version == "?":
            import re
            m = re.search(r'"([^"]+)"', line)
            if m:
                version = m.group(1)
                parts = version.split(".")
                try:
                    major = int(parts[0]) if parts[0] != "1" else int(parts[1])
                except (ValueError, IndexError):
                    major = 0

        # vendor
        if "openjdk" in low and vendor == "Unknown":
            vendor = "OpenJDK"
        if "hotspot" in low and "oracle" in low:
            vendor = "Oracle"
        if "temurin" in low or "adoptium" in low:
            vendor = "Eclipse Temurin"
        if "microsoft" in low:
            vendor = "Microsoft"
        if "graalvm" in low:
            vendor = "GraalVM"
        if "amazon" in low or "corretto" in low:
            vendor = "Amazon Corretto"
        if "azul" in low or "zulu" in low:
            vendor = "Azul Zulu"

    return version, vendor, major


def _probe(java_bin: str, source: str) -> Optional[JavaInstall]:
    """Ověří cestu java binary a vrátí JavaInstall nebo None."""
    if not java_bin or not Path(java_bin).is_file():
        return None
    out = _run([java_bin, "-version"])
    if not out:
        return None
    version, vendor, major = _parse_version(out)
    if major == 0:
        return None
    return JavaInstall(
        path=str(Path(java_bin).resolve()),
        version=version,
        vendor=vendor,
        major=major,
        source=source,
    )


# ── Windows ──────────────────────────────────────────────────────────────────

_WIN_SCAN_DIRS = [
    r"C:\Program Files\Java",
    r"C:\Program Files\Eclipse Adoptium",
    r"C:\Program Files\Microsoft",
    r"C:\Program Files\Amazon Corretto",
    r"C:\Program Files\Azul Systems\Zulu",
    r"C:\Program Files\Azul Systems",
    r"C:\Program Files\BellSoft",
    r"C:\Program Files\GraalVM",
    r"C:\Program Files\ojdkbuild",
    r"C:\Program Files (x86)\Java",
    # Prism Launcher bundled Java
    os.path.expandvars(r"%APPDATA%\PrismLauncher\java"),
    os.path.expandvars(r"%LOCALAPPDATA%\Packages\PrismLauncher\java"),
    # ATLauncher, MultiMC, CurseForge
    os.path.expandvars(r"%APPDATA%\ATLauncher\runtimes"),
    os.path.expandvars(r"%APPDATA%\MultiMC\java"),
]


def _scan_win_dirs() -> list[JavaInstall]:
    results = []
    for base in _WIN_SCAN_DIRS:
        p = Path(base)
        if not p.is_dir():
            continue
        for child in p.iterdir():
            java_bin = child / "bin" / "java.exe"
            inst = _probe(str(java_bin), "scan")
            if inst:
                results.append(inst)
    return results


def _scan_win_registry() -> list[JavaInstall]:
    results = []
    try:
        import winreg
        for root_key in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for reg_path in (
                r"SOFTWARE\JavaSoft\JDK",
                r"SOFTWARE\JavaSoft\Java Development Kit",
                r"SOFTWARE\JavaSoft\Java Runtime Environment",
                r"SOFTWARE\WOW6432Node\JavaSoft\JDK",
            ):
                try:
                    key = winreg.OpenKey(root_key, reg_path)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        ver_name = winreg.EnumKey(key, i)
                        i += 1
                        ver_key = winreg.OpenKey(key, ver_name)
                        try:
                            home, _ = winreg.QueryValueEx(ver_key, "JavaHome")
                            java_bin = Path(home) / "bin" / "java.exe"
                            inst = _probe(str(java_bin), "registry")
                            if inst:
                                results.append(inst)
                        except OSError:
                            pass
                        winreg.CloseKey(ver_key)
                    except OSError:
                        break
                winreg.CloseKey(key)
    except ImportError:
        pass
    return results


# ── Linux ────────────────────────────────────────────────────────────────────

_LIN_SCAN_DIRS = [
    "/usr/lib/jvm",
    "/usr/java",
    "/opt/java",
    "/opt/jdk",
]


def _scan_linux_dirs() -> list[JavaInstall]:
    results = []
    for base in _LIN_SCAN_DIRS:
        p = Path(base)
        if not p.is_dir():
            continue
        for child in p.iterdir():
            java_bin = child / "bin" / "java"
            inst = _probe(str(java_bin), "scan")
            if inst:
                results.append(inst)
    return results


def _scan_linux_alternatives() -> list[JavaInstall]:
    out = _run(["update-alternatives", "--list", "java"])
    if not out:
        return []
    results = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            inst = _probe(line, "alternatives")
            if inst:
                results.append(inst)
    return results


# ── Hlavní funkce ─────────────────────────────────────────────────────────────

def detect_java() -> list[JavaInstall]:
    """
    Vrátí seznam dostupných Java instalací seřazených od nejvyšší verze.
    Bez duplicit (stejná cesta).
    """
    found: dict[str, JavaInstall] = {}  # path → JavaInstall

    def add(inst: Optional[JavaInstall]):
        if inst and inst.path not in found:
            found[inst.path] = inst

    # 1. JAVA_HOME
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        exe = "java.exe" if sys.platform == "win32" else "java"
        add(_probe(str(Path(java_home) / "bin" / exe), "JAVA_HOME"))

    # 2. PATH
    java_in_path = shutil.which("java")
    if java_in_path:
        add(_probe(java_in_path, "PATH"))

    # 3. Platform scan
    if sys.platform == "win32":
        for inst in _scan_win_registry():
            add(inst)
        for inst in _scan_win_dirs():
            add(inst)
    else:
        for inst in _scan_linux_alternatives():
            add(inst)
        for inst in _scan_linux_dirs():
            add(inst)

    result = sorted(found.values(), key=lambda j: (-j.major, j.vendor))
    logger.info("detect_java: nalezeno %d instalací", len(result))
    return result
