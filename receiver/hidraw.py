"""Find the Amazon Fire TV remote's hidraw node.

The BLE remote's address can change after a re-pair (and a distro upgrade can
drop the bond), so a hard-coded MAC is brittle. Amazon remotes identify
themselves as Bluetooth HID vendor ``0x0171``; prefer that, and fall back to
the configured MAC only when no Amazon device is present.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

SYS_HIDRAW = Path("/sys/class/hidraw")
AMAZON_VENDOR = 0x0171


class RemoteNotFound(LookupError):
    """No matching hidraw node is present right now."""


def _uevent_fields(node: Path) -> dict[str, str]:
    try:
        text = (node / "device" / "uevent").read_text(encoding="utf-8")
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _hid_vendor(fields: dict[str, str]) -> int | None:
    """Parse ``HID_ID=bus:vendor:product`` (all hex) and return the vendor."""
    parts = fields.get("HID_ID", "").split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1], 16)
    except ValueError:
        return None


def find_remote_hidraw(
    mac: str | None = None,
    sysfs: str | Path = SYS_HIDRAW,
) -> str:
    """Return ``/dev/hidrawN`` for the Fire TV remote.

    With ``mac`` set, an exact ``HID_UNIQ`` match wins. Otherwise (or when it
    is absent) the first connected Amazon HID device is used, which survives
    re-pairing and upgraded BlueZ address handling.
    """
    want = (mac or "").strip().lower()
    if want in ("auto", "none"):
        want = ""

    amazon: list[tuple[str, str, str]] = []
    for node in sorted(glob.glob(str(Path(sysfs) / "hidraw*"))):
        fields = _uevent_fields(Path(node))
        if not fields:
            continue
        dev = f"/dev/{os.path.basename(node)}"
        uniq = fields.get("HID_UNIQ", "").lower()
        name = fields.get("HID_NAME", "")
        if want and uniq == want:
            return dev
        if _hid_vendor(fields) == AMAZON_VENDOR:
            amazon.append((dev, uniq, name))

    if amazon:
        # Prefer the device the remote calls "AR"; otherwise keep sysfs order.
        amazon.sort(key=lambda item: (item[2] != "AR", item[0]))
        return amazon[0][0]

    wanted = f" with HID_UNIQ={want}" if want else ""
    raise RemoteNotFound(
        f"No Amazon Fire TV remote hidraw node{wanted} found. "
        "Is the remote connected? (wake it; check: "
        f"grep -il 00000171 {sysfs}/hidraw*/device/uevent)"
    )
