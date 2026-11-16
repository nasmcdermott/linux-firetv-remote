#!/usr/bin/env python3
"""Fire remote button -> app/file/command launcher.

Reads the Fire TV remote's evdev keyboard device and, on a configured button
press, launches an action:
  * a ``.desktop`` file       -> ``gtk-launch`` or ``gio launch`` (honors the
                                entry's Exec=, like the app menu)
  * any existing file/dir     -> ``xdg-open`` (with gio/kde-open fallbacks)
  * anything else             -> run as a shell command (shlex.split)

Example config (config/button_launch.yaml):

    device: auto            # evdev path, or "auto" to detect by name
    grab: false             # EVIOCGRAB the device so the button stops doing
                            # its old desktop action (then map every button you
                            # still want here, or other clients lose it)
    cooldown: 0.4           # seconds between two dispatches of the same button
    scan_window: 0.2        # require MSC_SCAN within this many seconds
    buttons:
      - name: my-app
        key: KEY_PROGRAM          # evdev key name or numeric code
        scan: 0xc008d             # optional MSC_SCAN cross-check (your remote)
        action: ~/.local/share/applications/my-app.desktop

Usage:
    python3 scripts/remote_launch.py --config config/button_launch.yaml
    python3 scripts/remote_launch.py --list-devices

Needs read access to /dev/input/event* (the user is normally in the `input`
group). Runs as a systemd user service: see systemd/remote-launch.service.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import shlex
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML missing: install it (sudo dnf install python3-pyyaml / pip install pyyaml)"
    ) from exc

# linux/input-event-codes.h names this remote actually uses (extend as needed).
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

EV_KEY = 1
EV_MSC = 4
MSC_SCAN = 4
INPUT_EVENT = struct.Struct("llHHI")  # sec, usec, type, code, value (24 bytes)
EVIOCGRAB = 0x40044534
EVIOCGNAME = 0x80104506
EV_MAX_NAME = 256

LOCK_NAME = "remote-launch.lock"


def log(msg: str) -> None:
    print(f"remote-launch: {msg}", file=sys.stderr, flush=True)


def parse_key(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        return int(spec)
    upper = spec.upper()
    if upper in KEY_NAMES:
        return KEY_NAMES[upper]
    raise SystemExit(
        f"unknown key {spec!r}; use a numeric evdev code or one of "
        f"{sorted(KEY_NAMES)} (extend KEY_NAMES in scripts/remote_launch.py)"
    )


def parse_scan(spec: str) -> int:
    return int(str(spec).strip(), 0)


def list_devices() -> None:
    import glob

    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = ctypes.create_string_buffer(EV_MAX_NAME)
            fcntl.ioctl(fd, EVIOCGNAME, buf)
            name = buf.value.split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            name = "<unreadable>"
        finally:
            os.close(fd)
        print(f"{path}  {name}")


def _input_uevent(path: str) -> str:
    try:
        return Path(f"/sys/class/input/{Path(path).name}/device/uevent").read_text(
            errors="ignore"
        )
    except OSError:
        return ""


def is_amazon_remote(path: str) -> bool:
    """Match the remote by HID vendor/product, not its display name.

    The 0171:0427 remote's evdev nodes are named `AR Keyboard` and `AR`, not
    `Amazon Remote`. The old name check matched nothing and made the service
    restart every five seconds.
    """
    uevent = _input_uevent(path)
    if "HID_ID=0005:00000171:" in uevent or "v0171" in uevent.lower():
        return True
    for line in uevent.splitlines():
        if not line.startswith("PRODUCT="):
            continue
        # PRODUCT=bus/vendor/product/version, e.g. 5/171/427/69
        parts = line.split("=", 1)[1].split("/")
        if len(parts) >= 2 and parts[1].lower() == "171":
            return True
    return False


def find_device(preferred: str | None) -> str | None:
    import glob

    if preferred and preferred != "auto":
        return preferred
    candidates = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if not is_amazon_remote(path):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = ctypes.create_string_buffer(EV_MAX_NAME)
            fcntl.ioctl(fd, EVIOCGNAME, buf)
            name = buf.value.split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            name = ""
        finally:
            os.close(fd)
        candidates.append((path, name.lower()))
    if not candidates:
        return None
    # The keyboard interface carries the app keys; prefer it.
    for path, name in candidates:
        if "keyboard" in name:
            return path
    return candidates[0][0]


def wait_device(preferred: str | None) -> str:
    while True:
        path = find_device(preferred)
        if path:
            return path
        log("no Amazon Fire remote evdev device yet; retrying in 5s (wake/press a key)")
        time.sleep(5)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    buttons = []
    for b in cfg.get("buttons", []) or []:
        if "key" not in b or "action" not in b:
            raise SystemExit(f"button entry needs 'key' and 'action': {b}")
        buttons.append(
            {
                "name": str(b.get("name", f"button_{len(buttons)}")),
                "key": parse_key(str(b["key"])),
                "scan": parse_scan(b["scan"]) if "scan" in b else None,
                "action": str(b["action"]),
            }
        )
    return {
        "device": str(cfg.get("device", "auto")),
        "grab": bool(cfg.get("grab", False)),
        "cooldown": float(cfg.get("cooldown", 0.4)),
        "scan_window": float(cfg.get("scan_window", 0.2)),
        "buttons": buttons,
    }


def _open_path(path: str) -> None:
    for name in ("xdg-open", "gio", "kde-open5", "kde-open", "gnome-open"):
        opener = shutil.which(name)
        if not opener:
            continue
        argv = [opener, "open", path] if name == "gio" else [opener, path]
        try:
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            log(f"opened {path!r} via {name}")
            return
        except OSError as exc:
            log(f"{name} failed: {exc}")
    log(f"no opener found for {path!r} (xdg-open/gio/kde-open)")


def run_action(action: str) -> None:
    if action.lower().endswith(".desktop") and os.path.isfile(action):
        # Never xdg-open a .desktop file: that opens it in a text editor.
        desktop_id = os.path.basename(action)[:-len(".desktop")]
        if shutil.which("gtk-launch"):
            p = subprocess.Popen(
                ["gtk-launch", desktop_id], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                log(f"gtk-launch {desktop_id!r} rc={p.wait(timeout=5)}")
            except subprocess.TimeoutExpired:
                log(f"gtk-launch {desktop_id!r} running detached")
            return
        if shutil.which("gio"):
            p = subprocess.run(
                ["gio", "launch", action], capture_output=True, text=True,
                timeout=15, check=False,
            )
            log(f"gio launch rc={p.returncode} {p.stderr.strip()}")
            return
        log(f"cannot launch {action!r}: install gtk-launch or gio")
        return

    if os.path.exists(action):
        _open_path(action)
        return

    args = shlex.split(action)
    p = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
    log(f"ran {args!r} rc={p.returncode} {p.stderr.strip()}")


def acquire_singleton(directory: str | None = None):
    base = directory or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    fh = open(os.path.join(base, LOCK_NAME), "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


class Sender:
    """Per-key cooldown guard."""

    def __init__(self, cooldown: float):
        self.cooldown = cooldown
        self.last: dict[int, float] = {}

    def allowed(self, key: int) -> bool:
        now = time.monotonic()
        if now - self.last.get(key, 0.0) < self.cooldown:
            return False
        self.last[key] = now
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/button_launch.yaml",
                    help="YAML config (default: config/button_launch.yaml)")
    ap.add_argument("--device", help="evdev device path (overrides config)")
    ap.add_argument("--no-grab", action="store_true",
                    help="do not EVIOCGRAB the device (default: follow config)")
    ap.add_argument("--list-devices", action="store_true",
                    help="list /dev/input/event* names and exit")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return 0

    cfg = load_config(args.config)
    if not cfg["buttons"]:
        log(f"no buttons configured in {args.config}; nothing to watch")
        return 0

    device = args.device or cfg["device"]
    grab = cfg["grab"] and not args.no_grab
    path = wait_device(device)

    watch: dict[int, dict] = {}
    for b in cfg["buttons"]:
        watch[b["key"]] = b

    lock = acquire_singleton()
    if lock is None:
        log("another remote-launch holds the lock; exiting")
        return 0

    log(f"watching {path} ({len(watch)} button(s)), grab={grab}")
    for b in cfg["buttons"]:
        extra = f" scan 0x{b['scan']:x}" if b["scan"] is not None else ""
        log(f"  {b['name']}: key 0x{b['key']:x}{extra} -> {b['action']}")

    fd = os.open(path, os.O_RDONLY)
    grabbed = False
    try:
        if grab:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                grabbed = True
                log("device grabbed: this remote's other keys stop reaching the desktop")
            except OSError as e:
                log(f"could not grab device ({e}); continuing ungrabbed")

        last_scan: dict = {"value": None, "ts": 0.0}
        sender = Sender(cfg["cooldown"])

        while True:
            try:
                data = os.read(fd, INPUT_EVENT.size * 64)
            except InterruptedError:
                continue
            if not data:
                continue
            now = time.monotonic()
            for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                _sec, _usec, typ, code, value = INPUT_EVENT.unpack_from(data, off)

                if typ == EV_MSC and code == MSC_SCAN and value != 0:
                    last_scan["value"] = value
                    last_scan["ts"] = now
                    continue

                if typ != EV_KEY:
                    continue
                if value != 1:  # press only; ignore release (0) and repeat (2)
                    continue
                key_cfg = watch.get(code)
                if key_cfg is None:
                    continue

                if key_cfg["scan"] is not None:
                    sv = last_scan["value"]
                    if sv is None or now - last_scan["ts"] > cfg["scan_window"] \
                            or sv != key_cfg["scan"]:
                        log(
                            f"{key_cfg['name']}: press ignored "
                            f"(scan {sv:#x if sv is not None else None} != "
                            f"{key_cfg['scan']:#x})"
                        )
                        continue

                if not sender.allowed(key_cfg["key"]):
                    continue
                log(f"{key_cfg['name']}: press -> {key_cfg['action']}")
                run_action(key_cfg["action"])
    except KeyboardInterrupt:
        pass
    finally:
        if grabbed:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())