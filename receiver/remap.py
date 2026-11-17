"""Capture a Fire TV remote button and save it to button_launch.yaml.

The CLI wraps this module as ``firetv-remote remap``. It listens on the remote's
evdev keyboard interface, reports the evdev key and MSC_SCAN value, and writes a
mapping that ``scripts/remote_launch.py`` can execute.

Vendor app-shortcut buttons (Prime/Netflix/Disney/Peacock) can all report the
same evdev key (``KEY_PROGRAM``), so the MSC_SCAN value is what distinguishes
them. Capture stores it automatically for ``KEY_PROGRAM`` and whenever
``--scan`` is given explicitly.
"""

from __future__ import annotations

import glob
import os
import select
import shlex
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - pyyaml is a project dependency
    raise SystemExit("PyYAML missing: install the project requirements") from exc

EV_KEY = 1
EV_MSC = 4
MSC_SCAN = 4
KEY_PROGRAM = 362
INPUT_EVENT = struct.Struct("llHHI")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "button_launch.yaml"

KEY_NAMES = {
    "KEY_PROGRAM": 362,
    "KEY_MENU": 356,
    "KEY_HOME": 172,
    "KEY_BACK": 158,
    "KEY_SELECT": 331,
    "KEY_VOLUMEUP": 115,
    "KEY_VOLUMEDOWN": 114,
    "KEY_MUTE": 113,
    "KEY_PLAYPAUSE": 164,
}
NAME_BY_CODE = {code: name for name, code in KEY_NAMES.items()}

HEADER = """\
# Fire remote button mappings (config/button_launch.yaml).
# Managed by `firetv-remote remap`; edit freely.
#
# key    : evdev key name (KEY_PROGRAM) or numeric code (362).
# scan   : optional MSC_SCAN cross-check. Needed to distinguish app-shortcut
#          buttons that all report KEY_PROGRAM.
# action : a .desktop file, an existing file/directory, or a shell command.
"""

DEFAULT_BUTTONS: list[dict[str, Any]] = []


class RemapError(RuntimeError):
    """A capture or config error that should be shown to the user."""


@dataclass(frozen=True)
class CapturedButton:
    key: int
    key_name: str
    scan: int | None
    device: str


def key_name(code: int) -> str:
    return NAME_BY_CODE.get(code, f"KEY_0x{code:04x}")


def parse_key(spec: str | int) -> int:
    if isinstance(spec, int):
        return spec
    text = str(spec).strip()
    if text.isdigit():
        return int(text)
    upper = text.upper()
    if upper.startswith("0X"):
        return int(upper, 16)
    if upper.startswith("KEY_0X"):
        return int(upper[4:], 16)
    if upper in KEY_NAMES:
        return KEY_NAMES[upper]
    raise RemapError(f"unknown key {spec!r}; use --list-keys or a numeric evdev code")


def parse_scan(spec: str | int | None) -> int | None:
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    text = str(spec).strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError as exc:
        raise RemapError(f"invalid scan code {spec!r}; use hex like 0xc008d") from exc


def new_config() -> dict[str, Any]:
    return {
        "device": "auto",
        "grab": False,
        "cooldown": 0.4,
        "scan_window": 0.2,
        "buttons": [],
    }


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    target = Path(path).expanduser()
    cfg = new_config()
    if not target.exists():
        return cfg
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RemapError(f"cannot read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise RemapError(f"{target} must contain a YAML mapping")
    cfg.update(data)
    buttons = cfg.get("buttons")
    cfg["buttons"] = buttons if isinstance(buttons, list) else []
    return cfg


def save_config(cfg: dict[str, Any], path: str | Path = DEFAULT_CONFIG) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    target.write_text(HEADER + body, encoding="utf-8")
    return target


def _entry_identity(entry: dict[str, Any]) -> tuple[int, int | None] | None:
    try:
        key = parse_key(entry.get("key", ""))
    except RemapError:
        return None
    try:
        scan = parse_scan(entry.get("scan"))
    except RemapError:
        return None
    return key, scan


def _name_for_action(action: str) -> str:
    if action.lower().endswith(".desktop"):
        stem = Path(action).stem
    elif os.path.exists(action):
        stem = Path(action).name
    else:
        try:
            parts = shlex.split(action)
        except ValueError:
            parts = []
        if parts:
            words = [Path(parts[0]).stem]
            words.extend(word for word in parts[1:] if not word.startswith("-"))
            stem = "-".join(words[:3])
        else:
            stem = "action"
    clean = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in stem)
    return clean.strip("-._") or "action"


def add_mapping(
    cfg: dict[str, Any],
    *,
    key: str | int,
    action: str,
    scan: str | int | None = None,
    name: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Add or update a mapping. Returns (entry, created)."""
    action = action.strip()
    if not action:
        raise RemapError("action cannot be empty")
    key_code = parse_key(key)
    scan_code = parse_scan(scan)
    label = key_name(key_code)
    mapping_name = name.strip() if name else _name_for_action(action)
    if not mapping_name:
        mapping_name = _name_for_action(action)

    buttons = cfg.setdefault("buttons", [])
    if not isinstance(buttons, list):
        buttons = []
        cfg["buttons"] = buttons

    identity = (key_code, scan_code)
    if name is None:
        used = {str(entry.get("name", "")).strip() for entry in buttons}
        base_name = mapping_name
        suffix = 2
        while mapping_name in used:
            same = next(
                (entry for entry in buttons if str(entry.get("name", "")).strip() == mapping_name),
                None,
            )
            if same is not None and _entry_identity(same) == identity:
                break
            mapping_name = f"{base_name}-{suffix}"
            suffix += 1

    for entry in buttons:
        same_name = str(entry.get("name", "")).strip() == mapping_name
        # Without a scan code, KEY_PROGRAM matches any app-shortcut button; only
        # an explicit name should update such an entry. A scan-specific mapping
        # is unambiguous and can update by key+scan.
        same_button = scan_code is not None and _entry_identity(entry) == identity
        if not (same_name or same_button):
            continue
        if name is None:
            existing_name = str(entry.get("name", "")).strip()
            if existing_name:
                mapping_name = existing_name
        entry["name"] = mapping_name
        entry["key"] = label
        entry["action"] = action
        if scan_code is None:
            entry.pop("scan", None)
        else:
            entry["scan"] = f"0x{scan_code:x}"
        return entry, False

    entry = {"name": mapping_name, "key": label}
    if scan_code is not None:
        entry["scan"] = f"0x{scan_code:x}"
    entry["action"] = action
    buttons.append(entry)
    return entry, True


def remove_mapping(cfg: dict[str, Any], query: str) -> int:
    buttons = cfg.get("buttons") or []
    if not isinstance(buttons, list):
        return 0
    needle = str(query).strip().lower()
    kept = []
    removed = 0
    for entry in buttons:
        name = str(entry.get("name", "")).lower()
        key = str(entry.get("key", "")).lower()
        scan = entry.get("scan")
        try:
            scan_value = parse_scan(scan)
        except RemapError:
            scan_value = None
        scan_text = f"0x{scan_value:x}" if scan_value is not None else ""
        if needle in {name, key, str(scan).lower(), scan_text}:
            removed += 1
            continue
        kept.append(entry)
    cfg["buttons"] = kept
    return removed


def reset_config(cfg: dict[str, Any]) -> None:
    cfg["buttons"] = list(DEFAULT_BUTTONS)


def list_mappings(cfg: dict[str, Any]) -> None:
    buttons = cfg.get("buttons") or []
    if not buttons:
        print("No button mappings configured.")
        return
    for index, entry in enumerate(buttons, start=1):
        scan = entry.get("scan")
        if scan in (None, ""):
            scan_text = ""
        else:
            try:
                scan_text = f" scan=0x{parse_scan(scan):x}"
            except RemapError:
                scan_text = f" scan={scan}"
        print(
            f"{index:2}. {entry.get('name', '?')}  "
            f"key={entry.get('key', '?')}{scan_text}  -> {entry.get('action', '?')}"
        )


def _device_name(path: str) -> str:
    try:
        return Path(f"/sys/class/input/{Path(path).name}/device/name").read_text(
            encoding="utf-8", errors="ignore"
        ).strip()
    except OSError:
        return ""


def _input_uevent(path: str) -> str:
    try:
        return Path(f"/sys/class/input/{Path(path).name}/device/uevent").read_text(
            encoding="utf-8", errors="ignore"
        )
    except OSError:
        return ""


def is_amazon_remote(path: str) -> bool:
    uevent = _input_uevent(path)
    if "HID_ID=0005:00000171:" in uevent or "v0171" in uevent.lower():
        return True
    for line in uevent.splitlines():
        if not line.startswith("PRODUCT="):
            continue
        parts = line.split("=", 1)[1].split("/")
        if len(parts) >= 2 and parts[1].lower() == "171":
            return True
    return False


def find_remote_evdev(device: str | None = None) -> str:
    if device and device != "auto":
        return device
    candidates: list[tuple[str, str]] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if not is_amazon_remote(path):
            continue
        candidates.append((path, _device_name(path).lower()))
    if not candidates:
        raise RemapError(
            "no Amazon Fire remote evdev device found; wake/press a key and "
            "check that your user can read /dev/input/event*"
        )
    for path, name in candidates:
        if "keyboard" in name:
            return path
    return candidates[0][0]


def capture_button(device: str | None = None, timeout: float = 15.0) -> CapturedButton:
    path = find_remote_evdev(device)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError as exc:
        raise RemapError(
            f"cannot read {path}: permission denied. Add your user to the "
            "input group (then log out/in) or use a udev rule."
        ) from exc
    except OSError as exc:
        raise RemapError(f"cannot open {path}: {exc}") from exc

    deadline = time.monotonic() + timeout
    scan_value: int | None = None
    scan_ts = 0.0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemapError(f"timed out after {timeout:g}s waiting for a button press")
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except InterruptedError:
                continue
            if not ready:
                continue
            try:
                data = os.read(fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise RemapError(f"cannot read {path}: {exc}") from exc
            now = time.monotonic()
            for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                _sec, _usec, typ, code, value = INPUT_EVENT.unpack_from(data, off)
                if typ == EV_MSC and code == MSC_SCAN and value:
                    scan_value = value
                    scan_ts = now
                    continue
                if typ != EV_KEY or value != 1 or code == 0:
                    continue
                scan = scan_value
                if scan is not None and now - scan_ts > 0.5:
                    scan = None
                return CapturedButton(
                    key=code,
                    key_name=key_name(code),
                    scan=scan,
                    device=path,
                )
    finally:
        os.close(fd)


def list_desktops() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    dirs = [
        Path(os.path.expanduser("~/.local/share/applications")),
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
    ]
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            name = ""
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("Name="):
                        name = line.split("=", 1)[1].strip()
                        break
            except OSError:
                pass
            entries.append({"name": name or path.stem, "path": str(path), "file": path.name})
    return entries


def resolve_desktop(spec: str) -> str:
    entries = list_desktops()
    if str(spec).isdigit():
        index = int(spec) - 1
        if 0 <= index < len(entries):
            return entries[index]["path"]
        raise RemapError(f"no .desktop entry number {spec}; use --list-desktops")
    path = Path(spec).expanduser()
    if path.is_file():
        return str(path)
    needle = str(spec).lower()
    for entry in entries:
        if needle in {entry["file"].lower(), entry["name"].lower(), entry["path"].lower()}:
            return entry["path"]
    raise RemapError(f"no .desktop entry matching {spec!r}; use --list-desktops")


def restart_service() -> tuple[bool, str]:
    if not shutil.which("systemctl"):
        return False, "systemctl not found"
    proc = subprocess.run(
        ["systemctl", "--user", "restart", "remote-launch.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    message = (proc.stderr or proc.stdout).strip()
    return proc.returncode == 0, message
