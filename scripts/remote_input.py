#!/usr/bin/env python3
"""Fire remote input daemon: observes hidraw buttons and dispatches actions.

Watches the Fire TV remote's hidraw node for consumer-page button presses and
maps them to actions:

- menu      -> data-driven receiver/actions.py mapping; the default is the
               Plasma Bigscreen home overlay via D-Bus (activateLauncherMenu)

Volume, mute, and power are NOT handled here. cec-remote-observer.service
(fedora-cec-toolkit) owns them. It reads the remote's evdev events: vol/mute
arrive remapped as F16-F18 (one CEC step per press), and the real power button
arrives as KEY_POWER (one CEC wake per press). The voice button is a different
consumer code (0x0221) and is deliberately not mapped to anything here.
Handling any of these here too would double every action.

The Menu action goes through the same `receiver/actions.py` keymap as the
official phone app, so a custom `config/keymap.json` applies to both. The
default Menu mapping is D-Bus only; it sends no keystrokes, so a rebound
keyboard shortcut cannot break it.

Needs read access to the remote's /dev/hidrawN (run as root, or install
udev/91-fire-remote.rules and run as your user).

Usage:
    python3 scripts/remote_input.py --mac AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import fcntl
import os
import select
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from receiver.actions import ActionRunner, load_keymap  # noqa: E402
from receiver.hidraw import RemoteNotFound, find_remote_hidraw  # noqa: E402

SYS_HIDRAW = "/sys/class/hidraw"

# Consumer page (0x0C), little-endian 16-bit usage — hidraw report 0x02.
# See docs/remotes.md and scripts/poc_fire_voice.py for the full map.
MENU_KEY = 0x0040        # Menu button
VOLUME_UP = 0x00E9       # vol+
VOLUME_DOWN = 0x00EA     # vol-
MUTE = 0x00E2            # mute
POWER_KEY = 0x0030       # power (0x0066 on some SKUs)
POWER_KEY_ALT = 0x0066
HOME_KEY = 0x0223        # home / guide
VOICE_KEY_USAGE = 0x0221  # voice; owned by 06_voice_stt.py, ignored here

# Portable mode: when no cec-remote-observer owns volume/power, dispatch them
# through the shared receiver/actions.py keymap with local media keys.
PORTABLE_ACTIONS = {
    VOLUME_UP: "volume_up",
    VOLUME_DOWN: "volume_down",
    MUTE: "mute",
    POWER_KEY: "power",
    POWER_KEY_ALT: "power",
}

# Meta+F1 = Plasma Bigscreen home overlay global shortcut.
# Raw keycodes (linux/input-event-codes.h): leftmeta down, f1 down/up, leftmeta up.
# The Menu button needs no handling here: the remote's Menu key already reaches
# Plasma as KEY_MENU through the kernel hid->evdev path, and "Toggle Bigscreen
# Home Overlay" is bound to it in kglobalshortcutsrc. Key injection via ydotool
# was tried and rejected - from a systemd service a stuck Ctrl turned the
# intended Meta+O into Ctrl+O ("^O" printed to the focused terminal).

# Minimum gap between two dispatches of the SAME key (seconds). Guards against
# duplicate reports arriving in one buffered read.
ACTION_COOLDOWN: dict[int, float] = {
    MENU_KEY: 0.6,
    VOLUME_UP: 0.25,
    VOLUME_DOWN: 0.25,
    MUTE: 0.6,
    POWER_KEY: 0.6,
    POWER_KEY_ALT: 0.6,
}
# Token bucket: at most 8 dispatches per rolling second.
MAX_PER_SECOND = 8


def find_hidraw(mac: str | None = None) -> str:
    """Find the remote's /dev/hidrawN, by MAC or by Amazon HID vendor."""
    try:
        return find_remote_hidraw(mac)
    except RemoteNotFound as exc:
        raise SystemExit(str(exc)) from None


def log(msg: str) -> None:
    print(f"remote-input: {msg}", file=sys.stderr, flush=True)


class Sender:
    """Token bucket: at most MAX_PER_SECOND dispatches per rolling second."""

    def __init__(self, limit: int = MAX_PER_SECOND):
        self.limit = limit
        self.sent_ts: list[float] = []

    def allowed(self) -> bool:
        now = time.monotonic()
        self.sent_ts = [t for t in self.sent_ts if now - t < 1.0]
        if len(self.sent_ts) >= self.limit:
            return False
        self.sent_ts.append(now)
        return True


BUTTON_ACTIONS: dict[int, callable] = {}


def consumer_usage(payload: bytes) -> int | None:
    """Return the 16-bit LE consumer usage from a report 0x02 payload, or None."""
    if len(payload) < 2:
        return None
    return int.from_bytes(payload[:2], "little")


def parse_report_sizes(buf: bytes) -> dict[int, int]:
    """Return report-id -> input payload size in bytes (excluding the id)."""
    sizes: dict[int, int] = {}
    rid = rsize = rcount = 0
    i = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        if b == 0xFE:  # long item
            if i < len(buf):
                i += 2 + buf[i]
            continue
        tag, typ, size = b >> 4, (b >> 2) & 3, b & 3
        nbytes = (0, 1, 2, 4)[size]
        data = int.from_bytes(buf[i:i + nbytes], "little") if nbytes else 0
        i += nbytes
        if typ == 1:  # global
            if tag == 0x7:
                rsize = data
            elif tag == 0x9:
                rcount = data
            elif tag == 0x8:
                rid = data
        elif typ == 0 and tag == 0x8:  # main item: Input
            sizes[rid] = sizes.get(rid, 0) + rcount * rsize // 8
    return sizes


def iter_reports(data: bytes, sizes: dict[int, int]):
    """Split a hidraw read into (report-id, payload) records."""
    off = 0
    while off < len(data):
        rid = data[off]
        plen = sizes.get(rid)
        if plen is None or plen <= 0:
            return
        total = 1 + plen
        if off + total > len(data):
            return
        yield rid, data[off + 1:off + total]
        off += total


LOCK_NAME = "remote-input.lock"


def acquire_singleton(directory: str | None = None):
    """Hold an exclusive flock for the process lifetime; None if another holds it.

    A second copy (e.g. started by another systemd user manager) would send every
    Menu press to plasmashell twice, which toggles the overlay back off.
    """
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mac", default="",
                    help="remote BLE MAC (default: auto-detect the Amazon remote)")
    ap.add_argument("--hidraw", help="override hidraw device path")
    ap.add_argument("--portable", action="store_true",
                    help="handle volume/mute/power locally when no "
                         "cec-remote-observer owns them")
    args = ap.parse_args()
    lock = acquire_singleton()
    if lock is None:
        log("another remote-input holds the lock; exiting (no second Menu sender)")
        return 0

    sender = Sender()
    keymap = load_keymap()
    if args.portable:
        keymap["keys"].update({
            "volume_up": "key:volumeup",
            "volume_down": "key:volumedown",
            "mute": "key:mute",
            "power": "key:power",
        })
    runner = ActionRunner(keymap, live=True)
    last_action: dict[int, float] = {}  # last dispatch time per consumer usage

    BUTTON_NAMES: dict[int, str] = {
        MENU_KEY: "menu",
        VOLUME_UP: "volume_up",
        VOLUME_DOWN: "volume_down",
        MUTE: "mute",
        POWER_KEY: "power",
        POWER_KEY_ALT: "power",
    }

    try:
        while True:  # reconnect loop: the BLE node can vanish/reappear
            try:
                dev = args.hidraw or find_hidraw(args.mac)
            except SystemExit as e:
                log(f"{e}; retrying in 5s…")
                time.sleep(5)
                continue

            sizes: dict[int, int] = {}
            rdesc = Path(SYS_HIDRAW, os.path.basename(dev),
                         "device", "report_descriptor")
            try:
                sizes = parse_report_sizes(rdesc.read_bytes())
            except OSError as e:
                log(f".. cannot read report descriptor ({e}); "
                    "assuming consumer report 0x02 is 4 bytes")
                sizes = {0x02: 4}

            log(f"watching {dev}")
            try:
                fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                log(f"!! cannot open {dev}: {e}; retrying in 5s")
                time.sleep(5)
                continue

            lost = False
            try:
                while True:
                    try:
                        r, _, _ = select.select([fd], [], [], 1.0)
                    except InterruptedError:
                        continue
                    if not r:
                        continue

                    # Drain everything queued. A read can contain several
                    # concatenated reports; split by descriptor size so a
                    # keyboard report cannot swallow the following consumer
                    # release marker.
                    while True:
                        try:
                            data = os.read(fd, 4096)
                        except BlockingIOError:
                            break
                        except OSError as e:
                            log(f"hidraw lost ({e}); reconnecting…")
                            lost = True
                            break
                        if not data:
                            break

                        for rid, payload in iter_reports(data, sizes):
                            if rid != 0x02:
                                continue
                            usage = consumer_usage(payload)
                            if usage is None or usage == 0 or usage == VOICE_KEY_USAGE:
                                continue

                            now = time.monotonic()
                            name = BUTTON_NAMES.get(usage, f"usage_0x{usage:04x}")
                            cooldown = ACTION_COOLDOWN.get(usage, 0.25)
                            if now - last_action.get(usage, 0.0) < cooldown:
                                continue
                            last_action[usage] = now

                            if usage == MENU_KEY:
                                if not sender.allowed():
                                    log("menu: rate-guarded")
                                    continue
                                runner.key("menu")
                                continue

                            if args.portable and usage in PORTABLE_ACTIONS:
                                if not sender.allowed():
                                    log(f"button: {name} rate-guarded")
                                    continue
                                action = PORTABLE_ACTIONS[usage]
                                log(f"button: {name} (0x{usage:04x}) -> {action}")
                                runner.key(action)
                                continue

                            if usage not in BUTTON_ACTIONS:
                                log(f"button: {name} (0x{usage:04x}) press -> unmapped")
                                continue
                            if not sender.allowed():
                                log(f"button: {name} rate-guarded")
                                continue
                            log(f"button: {name} (0x{usage:04x}) press")
                            BUTTON_ACTIONS[usage]()
                    if lost:
                        break
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if lost:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
