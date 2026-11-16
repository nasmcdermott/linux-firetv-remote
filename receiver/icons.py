#!/usr/bin/env python3
"""Resolve and serve small PNG icons for the phone app's app row.

The official app takes ``tvIconArt`` from ``GET /v1/FireTV/apps`` and inserts
``._SX210_`` before the file extension (an Amazon CDN convention). The DIAL
HTTP server therefore accepts both ``name.png`` and ``name._SX210_.png``.

Icons come from, in order:

1. an explicit ``icon`` path in ``config/keymap.json`` (PNG/JPEG, or SVG if
   ImageMagick is available);
2. the desktop icon theme, matched from the app's ``desktop:<id>``;
3. a generated coloured placeholder with the app's initial.

Only the standard library is required. ImageMagick (``magick`` or
``convert``) is optional and used only for SVG sources or lettered
placeholders.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VECTOR_SUFFIXES = {".svg", ".svgz"}

ICON_DIRS = [
    Path.home() / ".local/share/icons/hicolor/256x256/apps",
    Path("/usr/share/icons/hicolor/256x256/apps"),
    Path("/usr/local/share/icons/hicolor/256x256/apps"),
    Path.home() / ".local/share/icons/hicolor/scalable/apps",
    Path("/usr/share/icons/hicolor/scalable/apps"),
    Path("/usr/share/icons/breeze/apps/64"),
    Path("/usr/share/icons/breeze/apps/48"),
    Path("/usr/share/icons/breeze-dark/apps/64"),
    Path("/usr/share/icons/breeze-dark/apps/48"),
    Path("/usr/share/icons/Adwaita/256x256/apps"),
    Path("/usr/share/icons/Adwaita/128x128/apps"),
    Path("/usr/share/icons/HighContrast/256x256/apps"),
]

# Desktop ids whose themed icon name differs from the id.
ICON_ALIASES = {
    "org.kde.konsole": ("konsole", "utilities-terminal"),
    "konsole": ("utilities-terminal",),
    "systemsettings": ("preferences-system", "systemsettings"),
    "org.kde.systemsettings": ("systemsettings", "preferences-system"),
    "org.kde.dolphin": ("system-file-manager", "dolphin"),
    "system settings": ("systemsettings", "preferences-system"),
    "files": ("system-file-manager", "org.kde.dolphin"),
    "terminal": ("utilities-terminal", "konsole"),
    "firefox": ("firefox",),
    "steam": ("steam",),
}

CACHE_DIR = Path.home() / ".cache" / "firetv-receiver" / "icons"


def desktop_id_from_action(action_kind: str, value: str) -> str | None:
    """Extract a likely icon name from a desktop/app action target."""
    if action_kind not in ("desktop", "app"):
        return None
    text = value.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.suffix == ".desktop":
        return path.stem
    if "/" in text or text.endswith((".png", ".svg", ".jpg", ".jpeg")):
        return None
    return text


def _candidate_names(desktop_id: str | None) -> list[str]:
    if not desktop_id:
        return []
    name = desktop_id.strip()
    name = name.removesuffix(".desktop")
    candidates = [name, name.lower()]
    candidates.extend(ICON_ALIASES.get(name, ()))
    candidates.extend(ICON_ALIASES.get(name.lower(), ()))
    if "." in name:
        tail = name.rsplit(".", 1)[-1]
        candidates.extend([tail, tail.lower()])
        candidates.extend(ICON_ALIASES.get(tail, ()))
        candidates.extend(ICON_ALIASES.get(tail.lower(), ()))
    # Keep order, drop duplicates.
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def find_icon(desktop_id: str | None) -> Path | None:
    """Find a themed icon for a desktop id, preferring PNG over SVG."""
    for directory in ICON_DIRS:
        if not directory.is_dir():
            continue
        for name in _candidate_names(desktop_id):
            for suffix in (".png", ".svg", ".svgz"):
                candidate = directory / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
    return None


def resolve_icon(icon_spec: object, desktop_id: str | None, app_id: str) -> Path | None:
    """Resolve an explicit icon path or a themed icon for the app."""
    if icon_spec:
        text = str(icon_spec).strip()
        if text.startswith(("http://", "https://")):
            return None  # external URL; the app fetches it directly
        path = Path(text).expanduser()
        if path.is_file():
            return path
    return find_icon(desktop_id) or find_icon(app_id)


def _convert(source: Path, size: int = 210) -> bytes | None:
    tool = shutil.which("magick") or shutil.which("convert")
    if not tool:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stat = source.stat()
    key = f"{source}:{stat.st_mtime_ns}:{stat.st_size}:{size}".encode()
    cache = CACHE_DIR / (hashlib.sha1(key).hexdigest() + ".png")
    if cache.is_file():
        return cache.read_bytes()
    try:
        result = subprocess.run(
            [
                tool,
                str(source),
                "-background",
                "none",
                "-resize",
                f"{size}x{size}",
                "png:-",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        return None
    cache.write_bytes(result.stdout)
    return result.stdout


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Minimal solid-colour PNG, no third-party libraries."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def placeholder_icon(label: str) -> bytes:
    """A coloured tile, with the first letter if ImageMagick is available."""
    letter = (label.strip() or "?").lstrip(". ")[:1].upper() or "?"
    digest = hashlib.sha1(label.encode("utf-8")).digest()
    rgb = (48 + digest[0] % 120, 48 + digest[1] % 120, 48 + digest[2] % 120)

    tool = shutil.which("magick") or shutil.which("convert")
    if tool:
        try:
            result = subprocess.run(
                [
                    tool,
                    "-size",
                    "210x210",
                    f"xc:rgb({rgb[0]},{rgb[1]},{rgb[2]})",
                    "-gravity",
                    "center",
                    "-pointsize",
                    "110",
                    "-fill",
                    "white",
                    "-annotate",
                    "0",
                    letter,
                    "png:-",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0 and result.stdout.startswith(b"\x89PNG"):
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _solid_png(210, 210, rgb)


def load_icon_bytes(path: Path | None, label: str) -> bytes:
    """Return PNG/JPEG bytes for an icon, converting SVG when possible."""
    if path is not None:
        suffix = path.suffix.lower()
        if suffix in RASTER_SUFFIXES:
            try:
                return path.read_bytes()
            except OSError:
                pass
        elif suffix in VECTOR_SUFFIXES:
            converted = _convert(path)
            if converted:
                return converted
    return placeholder_icon(label)


def icon_filename(app_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", app_id).strip("._") or "app"
    return f"{safe}.png"


def normalize_icon_path(path: str) -> str:
    """Map ``icon._SX210_.png`` (and friends) back to ``icon.png``."""
    path = path.split("?", 1)[0]
    return re.sub(r"\._S[XY]\d+_(?=\.[A-Za-z0-9]+$)", "", path)
