#!/usr/bin/env python3
"""Remap Fire remote buttons to .desktop entries.

Lists available .desktop files and updates config/button_launch.yaml so that
a chosen remote button launches the selected application.

Also supports removing mappings, resetting to defaults, and capturing
which button was pressed on the remote.

Usage (interactive):
    python3 scripts/remap_button.py

Usage (with arguments):
    python3 scripts/remap_button.py --desktop 36                    # select .desktop by number
    python3 scripts/remap_button.py --remove KEY_PROGRAM             # remove a button mapping
    python3 scripts/remap_button.py --reset                          # reset to defaults
    python3 scripts/remap_button.py --list-desktops                  # list .desktop files
    python3 scripts/remap_button.py --list-keys                      # list known key names
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Paths where .desktop files are searched
DESKTOP_DIRS = [
    os.path.expanduser("~/.local/share/applications"),
    "/usr/local/share/applications",
    "/usr/share/applications",
]

BUTTON_LAUNCH_YAML = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "button_launch.yaml"
)

# Default config: just the my-app button
DEFAULT_CONFIG = """# remote_launch.py config: remap Fire remote buttons to apps / files / commands.
#
# `key`   : evdev key name (KEY_PROGRAM) or numeric code (362).
# `scan`  : optional MSC_SCAN cross-check; your Fire remote's app keys
#           arrive as MSC_SCAN 0xc008d + KEY_PROGRAM.
# `action`: a .desktop file (launched via gtk-launch, honors its Exec=),
#           any existing file or directory (xdg-open), or a raw shell command.
device: auto            # evdev path, or "auto" to detect the Fire remote by name
grab: false             # set true to exclusively own the device (then map every
                        # button you still want here; other clients lose it)
cooldown: 0.4           # seconds between two dispatches of the same button
scan_window: 0.2        # MSC_SCAN must arrive within this many seconds of the key
buttons:
  - name: my-app
    key: KEY_PROGRAM      # code 362, MSC_SCAN 0xc008d on your remote
    scan: 0xc008d
    action: ~/.local/share/applications/my-app.desktop
"""


def find_desktop_files() -> list[dict[str, str]]:
    """Find all .desktop files in the standard directories.

    Returns a list of dicts with 'name' (display name) and 'path' (full path).
    """
    entries: list[dict[str, str]] = []
    for d in DESKTOP_DIRS:
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if not fname.endswith(".desktop"):
                continue
            fpath = os.path.join(d, fname)
            if not os.path.isfile(fpath):
                continue
            # Read the Name= field for display; fall back to filename
            name = ""
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("Name="):
                            name = line.split("=", 1)[1].strip()
                            break
            except OSError:
                pass
            if not name:
                name = fname
            entries.append({"name": name, "path": fpath, "file": fname})
    return entries


def list_desktops() -> None:
    """Print all available .desktop files with numbers."""
    entries = find_desktop_files()
    if not entries:
        print("No .desktop files found in standard directories.")
        return
    print("Available .desktop entries:")
    for i, e in enumerate(entries, start=1):
        print(f"  {i}. {e['name']}  ({e['file']})")
    print()
    print("You can use --desktop N (where N is the number) or --desktop path/to/file.desktop")


def get_desktop_by_number(num_str: str, entries: list[dict[str, str]]) -> dict | None:
    """Select a desktop entry by its display number."""
    try:
        idx = int(num_str) - 1
        if 0 <= idx < len(entries):
            return entries[idx]
    except ValueError:
        pass
    return None


def select_desktop_interactive(entries: list[dict[str, str]]) -> dict | None:
    """Interactive selection of a desktop entry."""
    if not entries:
        return None
    while True:
        list_desktops()
        choice = input("Enter the number of the .desktop entry (or 'q' to quit): ").strip()
        if choice.lower() == 'q':
            return None
        selected = get_desktop_by_number(choice, entries)
        if selected:
            return selected
        print("Invalid selection. Please try again.")


def parse_desktop_path(path_str: str, entries: list[dict[str, str]]) -> dict | None:
    """Select a desktop entry by its full or relative path."""
    # Check if it's an absolute path
    if os.path.isabs(path_str):
        if os.path.isfile(path_str) and path_str.endswith(".desktop"):
            # First try to find it in the known desktop dirs
            for e in find_desktop_files():
                if e["path"] == path_str or e["file"] == os.path.basename(path_str):
                    return e
            # If not found in known dirs but file exists, create a minimal entry
            return {"name": os.path.basename(path_str)[:-len(".desktop")],
                    "path": path_str,
                    }
    else:
        # Try relative path - search in desktop dirs
        for e in find_desktop_files():
            if e["file"] == path_str or e["name"] == path_str:
                return e
    # Try exact match by basename (for both absolute and relative)
    basename = os.path.basename(path_str)
    if basename.endswith(".desktop"):
        for e in find_desktop_files():
            if e["file"] == basename:
                return e
    return None


def update_config(action_path: str, button_name: str | None = None) -> None:
    """Update config/button_launch.yaml with the given .desktop action.

    If the file doesn't exist, create a minimal one. If a button with the given
    name already exists, update its action; otherwise append a new entry.
    """
    import yaml

    # Ensure config directory exists
    os.makedirs(os.path.dirname(BUTTON_LAUNCH_YAML), exist_ok=True)

    # If resetting to defaults, write the default config
    if action_path == "__reset__":
        with open(BUTTON_LAUNCH_YAML, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
        print(f"Config reset to defaults: {BUTTON_LAUNCH_YAML}")
        return

    # Load existing config or create minimal structure
    cfg: dict = {}
    if os.path.isfile(BUTTON_LAUNCH_YAML):
        with open(BUTTON_LAUNCH_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    # Buttons list
    buttons = cfg.setdefault("buttons", [])

    # Check if button --remove flag was given, remove matching button
    if button_name == "__remove__":
        # Remove all buttons with the given name
        original_len = len(buttons)
        buttons = [b for b in buttons if b.get("name") != action_path]
        removed = len(buttons) < original_len
        if removed:
            print(f"Removed button mapping(s) for: {action_path}")
        else:
            print(f"No button found with name: {action_path}")
        # Also remove the __remove/__reset markers from args
        # Write back
        with open(BUTTON_LAUNCH_YAML, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        print(f"Config written to: {BUTTON_LAUNCH_YAML}")
        return

    # If a button with this name already exists, update its action
    existing = None
    for b in buttons:
        if b.get("name") == button_name:
            existing = b
            break

    if existing:
        existing["action"] = action_path
        print(f"Updated button '{button_name}' action to: {action_path}")
    else:
        # Add new button entry
        new_button = {
            "name": button_name or "KEY_PROGRAM",
            "key": "KEY_PROGRAM",  # default; user can change later
            "action": action_path,
        }
        buttons.append(new_button)
        print(f"Added new button '{new_button['name']}' -> {action_path}")

    # Write back
    with open(BUTTON_LAUNCH_YAML, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    print(f"Config written to: {BUTTON_LAUNCH_YAML}")


def get_button_name_from_args_or_default(args, entries: list[dict[str, str]]) -> str:
    """Get the button name: from --button arg, or interactive input, or default."""
    if args.button:
        return args.button
    # Try non-interactive input if stdin is a terminal
    if os.isatty(sys.stdin.fileno()):
        try:
            name = input("Enter button name (or press ENTER to use default): ").strip()
            if name:
                return name
        except (EOFError, KeyboardInterrupt):
            pass
    return "KEY_PROGRAM"


def capture_button_press(timeout: float = 10.0) -> str | None:
    """Capture a single button press from the Fire remote.

    Listens on the evdev device and returns the key name of the first press.
    Returns None on timeout or error.
    """
    import glob
    import struct

    EV_KEY = 1

    # Find the Fire remote evdev device
    INPUT_GLOB = "/dev/input/event*"
    remote_vids = ["0171"]

    def is_remote_node(dev: str) -> bool:
        try:
            with open(os.path.join("/sys/class/input", os.path.basename(dev),
                                    "device", "uevent")) as f:
                uevent = f.read().lower()
            return any(v in uevent for v in remote_vids)
        except OSError:
            return False

    # Open the first remote device found
    for path in sorted(glob.glob(INPUT_GLOB)):
        if not is_remote_node(path):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            print(f"Listening for button presses on {path}... (press a button on your remote)")
            start = time.monotonic()
            while True:
                try:
                    data = os.read(fd, 64)
                except (BlockingIOError, OSError):
                    continue
                if not data:
                    if time.monotonic() - start > timeout:
                        print("Timeout waiting for button press.")
                        os.close(fd)
                        return None
                    continue

                # Parse input event: struct input_event = {time, type, code, value}
                off = 0
                while off + 24 <= len(data):
                    _sec, _usec, typ, code, value = struct.unpack_from("llHHI", data, off)
                    off += 24

                    if typ == EV_KEY and value == 1:  # key press (not release/repeat)
                        # Map known key codes to names
                        key_names = {
                            0x01: "KEY_ESC", 0x1e: "KEY_1", 0x1f: "KEY_2", 0x20: "KEY_3",
                            0x21: "KEY_4", 0x22: "KEY_5", 0x23: "KEY_6", 0x24: "KEY_7",
                            0x25: "KEY_8", 0x26: "KEY_9", 0x27: "KEY_0", 0x28: "KEY_RETURN",
                            0x29: "KEY_ESC", 0x2a: "KEY_DELETE", 0x3b: "KEY_TAB", 0x3c: "KEY_Q",
                            0x3d: "KEY_W", 0x3e: "KEY_E", 0x3f: "KEY_R", 0x40: "KEY_T",
                            0x41: "KEY_Y", 0x42: "KEY_U", 0x43: "KEY_I", 0x44: "KEY_O",
                            0x45: "KEY_P", 0x46: "KEY_A", 0x47: "KEY_S", 0x48: "KEY_D",
                            0x49: "KEY_F", 0x4a: "KEY_G", 0x4b: "KEY_H", 0x4c: "KEY_J",
                            0x4d: "KEY_K", 0x4e: "KEY_L", 0x4f: "KEY_SEMICOLON", 0x50: "KEY_EQUAL",
                            0x51: "KEY_BACKSPACE", 0x52: "KEY_TAB", 0x53: "KEY_Q", 0x54: "KEY_W",
                            0x55: "KEY_E", 0x56: "KEY_R", 0x57: "KEY_T", 0x58: "KEY_Y",
                            0x59: "KEY_U", 0x5a: "KEY_I", 0x5b: "KEY_O", 0x5c: "KEY_P",
                            0x5d: "KEY_A", 0x5e: "KEY_S", 0x5f: "KEY_D", 0x60: "KEY_F",
                            0x61: "KEY_G", 0x62: "KEY_H", 0x63: "KEY_J", 0x64: "KEY_K",
                            0x65: "KEY_L", 0x66: "KEY_RETURN", 0x67: "KEY_CTRL_LEFT",
                            0x68: "KEY_SHIFT_LEFT", 0x69: "KEY_ALT_LEFT", 0x6a: "KEY_SPACE",
                            0x6b: "KEY_CAPSLOCK", 0x6c: "KEY_F2", 0x6d: "KEY_F3", 0x6e: "KEY_F4",
                            0x6f: "KEY_F5", 0x70: "KEY_F6", 0x71: "KEY_F7", 0x72: "KEY_F8",
                            0x73: "KEY_F9", 0x74: "KEY_F10", 0x75: "KEY_NUMLOCK",
                            0x76: "KEY_SCROLLLOCK",
                            0x77: "KEY_SCROLLLOCK", 0x78: "KEY_F11", 0x79: "KEY_F12",
                        }
                        key_name = key_names.get(code, f"KEY_0x{code:04x}")
                        print(f"Captured: {key_name} (code={code})")
                        os.close(fd)
                        return key_name
                # Check for MSC_SCAN (app key)
                if typ == 4 and code == 0x008d:  # MSC_SCAN
                    print("Captured app key (MSC_SCAN 0xc008d)")
                    os.close(fd)
                    return "KEY_PROGRAM"

                # Continue to next event if we didn't match
                break
        except OSError as e:
            print(f"Could not open {path}: {e}")
            continue

    return None


def remove_button_mapping(button_name: str) -> None:
    """Remove a button mapping from the config."""
    import yaml

    cfg: dict = {}
    if os.path.isfile(BUTTON_LAUNCH_YAML):
        with open(BUTTON_LAUNCH_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    buttons = cfg.setdefault("buttons", [])
    original_len = len(buttons)
    # Remove buttons where name MATCHES button_name
    cfg["buttons"] = [b for b in buttons if b.get("name") != button_name]

    if len(cfg["buttons"]) < original_len:
        with open(BUTTON_LAUNCH_YAML, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        print(f"Removed button mapping for: {button_name}")
    else:
        print(f"No button found with name: {button_name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--button", default=None,
                    help="Button key to remap (default: KEY_PROGRAM). Use --list-keys for names.")
    ap.add_argument("--desktop", default=None,
                    help="Path or number of .desktop file. If not given, interactive picker.")
    ap.add_argument("--capture", action="store_true",
                    help="Capture a button press from the Fire remote and use it as the button")
    ap.add_argument("--remove", default=None,
                    help="Remove the button mapping for the given button name")
    ap.add_argument("--reset", action="store_true",
                    help="Reset config to defaults (only my-app button)")
    ap.add_argument("--list-desktops", action="store_true",
                    help="List available .desktop files and exit")
    ap.add_argument("--list-keys", action="store_true",
                    help="List known key names from remote_launch.py")
    args = ap.parse_args()

    # --list-keys takes highest priority
    if args.list_keys:
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
        print("Known key names:")
        for k, v in sorted(KEY_NAMES.items(), key=lambda x: x[1]):
            print(f"  {k}: {v}")
        return 0

    # --reset takes second priority (after --list-keys)
    if args.reset:
        update_config("__reset__")
        return 0

    # --remove 
    if args.remove:
        remove_button_mapping(args.remove)
        return 0

    # List desktops if requested
    if args.list_desktops:
        list_desktops()
        return 0

    # Find .desktop entries
    entries = find_desktop_files()

    # Determine which desktop to use
    selected: dict | None = None

    if args.desktop:
        # If it's a number, use the list index
        if args.desktop.isdigit():
            selected = get_desktop_by_number(args.desktop, entries)
        else:
            selected = parse_desktop_path(args.desktop, entries)
        if not selected:
            print(f"ERROR: Could not find .desktop entry: {args.desktop}")
            list_desktops()
            return 1
    else:
        # Interactive selection
        selected = select_desktop_interactive(entries)
        if not selected:
            print("No .desktop entry selected. Exiting.")
            return 0

    action_path = selected["path"]
    print(f"\nSelected: {selected['name']} -> {action_path}")

    # Get button name
    # If --capture is used, first capture the button press
    if args.capture:
        captured = capture_button_press(timeout=10.0)
        if captured:
            # Try to map the captured key name to our known names
            button_name = captured  # use the captured name as-is
        else:
            print("No button captured. Using default KEY_PROGRAM.")
            button_name = "KEY_PROGRAM"
    else:
        button_name = get_button_name_from_args_or_default(args, entries)

    update_config(action_path, button_name=button_name)

    # Provide next steps
    print("\n--- Next steps ---")
    print("1. Review config/button_launch.yaml to verify the changes")
    print("2. Run remote_launch.py to test the new mapping:")
    print("   python3 scripts/remote_launch.py --config config/button_launch.yaml")
    print("3. To make it persistent, consider adding a systemd user service:")
    print("   cp systemd/remote-launch.service ~/.config/systemd/user/")
    print("   systemctl --user enable --now remote-launch.service")

    return 0


if __name__ == "__main__":
    sys.exit(main())
