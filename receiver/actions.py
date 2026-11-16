#!/usr/bin/env python3
"""Map Fire TV app actions to Linux desktop actions.

The official Fire TV app sends abstract actions (``dpad_up``, ``select``,
``menu``, ``volume_up``, ...). This module turns them into something the
Linux desktop understands, using the same conventions as the sibling
``fire-remote-client`` project:

* D-pad / select / back  -> arrow keys, Enter and Escape through ``ydotool``
* Menu                  -> Plasma's ``activateLauncherMenu`` D-Bus method
* Volume / mute / power -> ``cec-policy`` (HDMI-CEC, like the BLE remote)
* Text                  -> ``ydotool type``
* App shortcuts         -> ``gtk-launch`` (honours the .desktop ``Exec=``)

Every mapping is data, not code. The defaults live in ``DEFAULT_KEYMAP`` and
an optional JSON file can override any entry. Run::

    python3 -m receiver.actions --print-defaults
    python3 -m receiver.actions --self-test

No third-party Python packages are required. ``ydotool``, ``busctl``,
``cec-policy``, ``gtk-launch`` and ``xdg-open`` are used only when an action
is actually executed.
"""

from __future__ import annotations

import argparse
import copy
import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Linux key codes (linux/input-event-codes.h). ydotool speaks raw key codes.

KEYCODES: dict[str, int] = {
    "esc": 1,
    "escape": 1,
    "backspace": 14,
    "tab": 15,
    "enter": 28,
    "return": 28,
    "space": 57,
    "capslock": 58,
    # Letters and digits, for combinations such as "key:leftmeta+w".
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "minus": 12,
    "equal": 13,
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
    "comma": 51,
    "dot": 52,
    "slash": 53,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "f13": 183,
    "f14": 184,
    "f15": 185,
    "f16": 186,
    "f17": 187,
    "f18": 188,
    "f19": 189,
    "f20": 190,
    "f21": 191,
    "f22": 192,
    "f23": 193,
    "f24": 194,
    "leftctrl": 29,
    "ctrl": 29,
    "rightctrl": 97,
    "leftshift": 42,
    "shift": 42,
    "rightshift": 54,
    "leftalt": 56,
    "alt": 56,
    "rightalt": 100,
    "leftmeta": 125,
    "super": 125,
    "meta": 125,
    "rightmeta": 126,
    "home": 102,
    "end": 107,
    "up": 103,
    "down": 108,
    "left": 105,
    "right": 106,
    "pageup": 104,
    "pagedown": 109,
    "insert": 110,
    "delete": 111,
    "mute": 113,
    "volumedown": 114,
    "volumeup": 115,
    "power": 116,
    "pause": 119,
    "stop": 166,
    "previoussong": 165,
    "nextsong": 163,
    "playpause": 164,
    "rewind": 168,
    "fastforward": 208,
    "homepage": 172,
    "refresh": 173,
    "menu": 139,
    "sleep": 142,
    "wakeup": 143,
    "back": 158,
    "forward": 159,
    "browser": 150,
    "epg": 365,
    "print": 210,
}

# Canonical display name for logging (first name wins).
KEY_NAMES: dict[int, str] = {}
for _name, _code in KEYCODES.items():
    KEY_NAMES.setdefault(_code, _name)


# ---------------------------------------------------------------------------
# Default mapping. Keep this JSON-compatible: it is what --print-defaults
# emits and what an override file merges into.

DEFAULT_KEYMAP: dict[str, dict[str, Any]] = {
    "keys": {
        # Navigation. The app sends keyDown/keyUp for these, so hold them.
        "dpad_up": "key:up",
        "dpad_down": "key:down",
        "dpad_left": "key:left",
        "dpad_right": "key:right",
        "select": "key:enter",
        # Back. Escape closes dialogs and overlays. Use "key:back"
        # (KEY_BACK=158) if you would rather mimic the BLE remote's raw key.
        "back": "key:esc",
        # Home: the physical remote's Home key is KEY_HOMEPAGE (172), which
        # Plasma binds to "Toggle Bigscreen Home Screen". The app can send a
        # keyDown without a matching keyUp, so never hold this one.
        "home": {"type": "key", "key": "homepage", "hold": False},
        # Menu: the same D-Bus call fire-remote-client uses for Bigscreen.
        "menu": (
            "dbus:org.kde.plasmashell,/PlasmaShell,"
            "org.kde.PlasmaShell,activateLauncherMenu"
        ),
        "play": "key:playpause",
        # Volume/mute/power follow fire-remote-client: HDMI-CEC, not the
        # local sink. For local desktop volume use "key:volumeup",
        # "key:volumedown" and "key:mute" instead.
        "volume_up": "command:cec-policy volume up",
        "volume_down": "command:cec-policy volume down",
        "mute": "command:cec-policy volume mute",
        # The physical power button does two things: CEC wake (TV on) and
        # KEY_POWER to the OS (Plasma's sleep/logout/restart/shutdown menu).
        "power": {
            "type": "sequence",
            "actions": [
                "command:cec-policy wake --reason powerkey",
                "key:power",
            ],
        },
        # The app sends these automatically around a session. No-op by
        # default so opening the app cannot wake the TV unexpectedly.
        # Try "command:cec-policy wake --reason powerkey" for wakeup and
        # "command:cec-policy standby" for sleep if you want CEC there.
        "wakeup": "noop",
        "sleep": "noop",
        "backspace": "key:backspace",
        "epg": "key:epg",
    },
    "media": {
        "play": "key:playpause",
        "scan_forward": "key:fastforward",
        "scan_back": "key:rewind",
    },
    "apps": {
        # appId -> action, or an object with action/name/icon/advertise.
        # "advertise": false keeps an entry launchable without showing it in
        # the phone's app row (the gear/settings entry does this).
        "settings": {
            "action": "app:System Settings",
            "name": "Settings",
            "advertise": False,
        },
        "linux.firefox": {
            "action": "app:Firefox",
            "name": "Firefox",
        },
        "linux.steam": {
            "action": "app:Steam",
            "name": "Steam",
        },
        "linux.terminal": {
            "action": "app:Konsole",
            "name": "Terminal",
        },
    },
}

DEFAULT_KEYMAP_PATH = Path(__file__).resolve().parent.parent / "config" / "keymap.json"

# A held key that never receives its keyUp is released after this many
# seconds. Guards against a dropped request or a phone leaving the network.
HOLD_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Action parsing

@dataclass(frozen=True)
class Action:
    kind: str
    keys: tuple[int, ...] = ()
    command: tuple[str, ...] = ()
    text: str = ""
    desktop: str = ""
    app_name: str = ""
    dbus: tuple[str, str, str, str] = ()
    sequence: tuple[Action, ...] = ()
    hold: bool = True


def _parse_key_list(raw: Any) -> tuple[int, ...]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    elif isinstance(raw, str):
        items = [part for part in raw.replace(" ", "+").split("+") if part]
    else:
        raise TypeError(f"key must be a name, list or '+'-joined string, got {raw!r}")

    codes: list[int] = []
    for item in items:
        name = item.strip().lower()
        if not name:
            continue
        if name.isdigit():
            codes.append(int(name))
            continue
        try:
            codes.append(KEYCODES[name])
        except KeyError:
            raise ValueError(f"unknown key {item!r}; see KEYCODES in receiver/actions.py") from None
    if not codes:
        raise ValueError("key action has no keys")
    return tuple(codes)


def _parse_command(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = shlex.split(raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        raise TypeError(f"command must be a string or list, got {raw!r}")
    if not parts:
        raise ValueError("command action is empty")
    return tuple(parts)


def parse_action(spec: Any) -> Action:
    """Turn a keymap entry (string shorthand or object) into an Action."""
    if isinstance(spec, dict):
        kind = str(spec.get("type", "")).strip().lower()
        hold = bool(spec.get("hold", True))
        if kind == "key":
            return Action("key", keys=_parse_key_list(spec.get("key", spec.get("keys"))), hold=hold)
        if kind == "command":
            return Action("command", command=_parse_command(spec.get("command")), hold=hold)
        if kind == "text":
            return Action("text", text=str(spec.get("text", "")), hold=hold)
        if kind == "desktop":
            value = str(spec.get("desktop", spec.get("id", ""))).strip()
            if not value:
                raise ValueError("desktop action needs an id or path")
            return Action("desktop", desktop=value, hold=hold)
        if kind == "app":
            value = str(spec.get("app", spec.get("name", ""))).strip()
            if not value:
                raise ValueError("app action needs an app name")
            return Action("app", app_name=value, hold=hold)
        if kind == "dbus":
            fields = tuple(
                str(spec.get(name, "")).strip()
                for name in ("service", "path", "interface", "method")
            )
            if not all(fields):
                raise ValueError("dbus action needs service, path, interface and method")
            return Action("dbus", dbus=fields, hold=hold)  # type: ignore[arg-type]
        if kind == "sequence":
            raw_actions = spec.get("actions")
            if not isinstance(raw_actions, (list, tuple)) or not raw_actions:
                raise ValueError("sequence action needs a non-empty 'actions' list")
            return Action("sequence", sequence=tuple(parse_action(item) for item in raw_actions))
        if kind in ("noop", "none", ""):
            return Action("noop")
        raise ValueError(f"unknown action type {kind!r}")

    if not isinstance(spec, str):
        raise TypeError(f"action must be a string or object, got {spec!r}")

    text = spec.strip()
    if not text or text.lower() in ("noop", "none"):
        return Action("noop")
    kind, sep, rest = text.partition(":")
    if not sep:
        raise ValueError(f"action {spec!r} is missing a 'type:' prefix")
    kind = kind.strip().lower()
    rest = rest.strip()
    if kind in ("key", "keys"):
        return Action("key", keys=_parse_key_list(rest))
    if kind == "command":
        return Action("command", command=_parse_command(rest))
    if kind == "text":
        return Action("text", text=rest)
    if kind == "desktop":
        if not rest:
            raise ValueError("desktop action needs an id or path")
        return Action("desktop", desktop=rest)
    if kind == "app":
        if not rest:
            raise ValueError("app action needs an app name")
        return Action("app", app_name=rest)
    if kind == "dbus":
        fields = tuple(part.strip() for part in rest.split(","))
        if len(fields) != 4 or not all(fields):
            raise ValueError(
                "dbus action must be "
                "dbus:service,path,interface,method"
            )
        return Action("dbus", dbus=fields)  # type: ignore[arg-type]
    raise ValueError(f"unknown action type {kind!r}")


def describe_action(action: Action) -> str:
    if action.kind == "key":
        names = "+".join(KEY_NAMES.get(code, str(code)) for code in action.keys)
        suffix = "" if action.hold else " (tap only)"
        return f"key {names}{suffix}"
    if action.kind == "command":
        return "command " + " ".join(shlex.quote(part) for part in action.command)
    if action.kind == "text":
        return f"text {action.text!r}"
    if action.kind == "desktop":
        return f"desktop {action.desktop}"
    if action.kind == "app":
        return f"app {action.app_name}"
    if action.kind == "dbus":
        return "dbus " + " ".join(action.dbus)
    if action.kind == "sequence":
        return " then ".join(describe_action(step) for step in action.sequence)
    return "noop"


def app_action_spec(spec: Any) -> Any:
    """Return the action spec from an app entry.

    An app entry may be a plain action ("desktop:firefox") or an object with
    metadata: {"action": ..., "name": "Firefox", "icon": "/path", ...}.
    A real action object such as {"type": "key", ...} is passed through.
    """
    if isinstance(spec, dict) and "type" not in spec:
        if "action" not in spec:
            raise ValueError("app entry needs an 'action' field")
        return spec["action"]
    return spec


def advertised_apps(keymap: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return app entries that should appear in the phone's app row."""
    entries: list[dict[str, Any]] = []
    for app_id, spec in keymap["apps"].items():
        if isinstance(spec, dict) and "type" not in spec:
            if spec.get("advertise", True) is False:
                continue
            name = str(spec.get("name") or app_id)
            icon = spec.get("icon")
        else:
            name = app_id
            icon = None
        entries.append(
            {
                "appId": app_id,
                "name": name,
                "icon": icon,
                "action": parse_action(app_action_spec(spec)),
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Keymap loading

def load_keymap(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return defaults merged with an optional JSON override.

    The override is shallow per section: you can redefine only ``select``
    without repeating every other key.
    """
    keymap = copy.deepcopy(DEFAULT_KEYMAP)
    source = path if path is not None else DEFAULT_KEYMAP_PATH
    if source is None or not source.exists():
        return keymap

    try:
        override = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read keymap {source}: {exc}") from None
    if not isinstance(override, dict):
        raise SystemExit(f"keymap {source} must contain a JSON object")

    for section in ("keys", "media", "apps"):
        values = override.get(section)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise SystemExit(f"keymap section {section!r} must be an object")
        # Validate now, not on the first button press.
        for name, spec in values.items():
            try:
                parse_action(app_action_spec(spec) if section == "apps" else spec)
            except (ValueError, TypeError) as exc:
                raise SystemExit(f"keymap {source}: {section}.{name}: {exc}") from None
        keymap[section].update(values)
    return keymap


def have_ydotool() -> bool:
    return shutil.which("ydotool") is not None


def _print_flush(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Action execution

class ActionRunner:
    """Execute mapped actions, optionally for real.

    ``live=False`` logs what would happen and never touches the desktop.
    """

    def __init__(
        self,
        keymap: dict[str, dict[str, Any]],
        live: bool = False,
        log: Callable[[str], None] = _print_flush,
    ) -> None:
        self.keymap = keymap
        self.live = live
        self.log = log
        self._lock = threading.RLock()
        self._held: dict[int, float] = {}
        self._app_handler: Any = None
        self._stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="key-watchdog", daemon=True
        )
        self._watchdog.start()

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._stop.set()
        self.release_all()

    def release_all(self) -> None:
        """Release every key this runner is holding."""
        with self._lock:
            if not self._held:
                return
            codes = list(reversed(self._held))
            self._held.clear()
            if self.live and have_ydotool():
                self._ydotool(["key", *[f"{code}:0" for code in codes]])
            self.log("[action] released held keys")

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(0.5):
            now = time.monotonic()
            with self._lock:
                expired = [code for code, deadline in self._held.items() if now >= deadline]
                for code in expired:
                    self._held.pop(code, None)
                    if self.live and have_ydotool():
                        self._ydotool(["key", f"{code}:0"])
                    self.log(
                        f"[action] auto-released {KEY_NAMES.get(code, code)} "
                        f"after {HOLD_TIMEOUT_SECONDS:.0f}s (missing keyUp)"
                    )

    # -- public dispatch -------------------------------------------------

    def key(self, name: str, key_action_type: str | None = None) -> None:
        spec = self.keymap["keys"].get(name)
        if spec is None:
            self.log(f"[action] no mapping for key action {name!r}")
            return
        try:
            action = parse_action(spec)
        except (ValueError, TypeError) as exc:
            self.log(f"[action] bad mapping for {name!r}: {exc}")
            return

        if action.kind != "key":
            # Commands / D-Bus / launchers fire once, on press.
            if key_action_type == "keyUp":
                return
            self.log(f"[action] {name} -> {describe_action(action)}")
            self._execute(action)
            return

        self.log(f"[action] {name} {key_action_type or 'tap'} -> {describe_action(action)}")
        if not action.hold:
            if key_action_type != "keyUp":
                self._tap(action.keys)
            return
        if key_action_type == "keyDown":
            self._down(action.keys)
        elif key_action_type == "keyUp":
            self._up(action.keys)
        else:
            self._tap(action.keys)

    def media(self, name: str, body: dict[str, Any]) -> None:
        if name == "play":
            spec: Any = self.keymap["media"].get("play")
            label = "media.play"
        elif name in ("scan", "seek"):
            direction = str(body.get("direction", "")).strip().lower()
            if direction in ("back", "backward", "rewind"):
                direction = "back"
            elif direction in ("forward", "fastforward", "fast_forward"):
                direction = "forward"
            spec = self.keymap["media"].get(f"scan_{direction}")
            label = f"media.scan({direction or '?'})"
        else:
            self.log(f"[action] no mapping for media action {name!r}")
            return
        if spec is None:
            self.log(f"[action] no mapping for {label}")
            return
        try:
            action = parse_action(spec)
        except (ValueError, TypeError) as exc:
            self.log(f"[action] bad mapping for {label}: {exc}")
            return
        self.log(f"[action] {label} -> {describe_action(action)}")
        self._execute(action)

    def text(self, value: str) -> None:
        if not value:
            return
        action = Action("text", text=value)
        self.log(f"[action] text -> {value!r}")
        self._execute(action)

    def app(self, app_id: str) -> None:
        spec = self.keymap["apps"].get(app_id)
        if spec is None:
            self.log(f"[action] no mapping for app {app_id!r}")
            return
        try:
            action = parse_action(app_action_spec(spec))
        except (ValueError, TypeError) as exc:
            self.log(f"[action] bad mapping for app {app_id!r}: {exc}")
            return
        self.log(f"[action] app {app_id!r} -> {describe_action(action)}")
        self._execute(action)

    # -- key injection ---------------------------------------------------

    def _ydotool(self, args: list[str], timeout: float = 2.0) -> None:
        try:
            result = subprocess.run(
                ["ydotool", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.log(f"[action] ydotool failed: {exc}")
            return
        if result.returncode != 0:
            self.log(
                f"[action] ydotool {' '.join(args)} failed rc={result.returncode}: "
                f"{result.stderr.strip()}"
            )

    def _tap(self, codes: tuple[int, ...]) -> None:
        if not self.live:
            self.log("  (dry run; --inject ydotool to execute)")
            return
        if not have_ydotool():
            self.log("[action] ydotool is not installed")
            return
        events = [f"{code}:1" for code in codes]
        events += [f"{code}:0" for code in reversed(codes)]
        with self._lock:
            self._ydotool(["key", *events])

    def _down(self, codes: tuple[int, ...]) -> None:
        if not self.live:
            self.log("  (dry run; --inject ydotool to execute)")
            return
        if not have_ydotool():
            self.log("[action] ydotool is not installed")
            return
        with self._lock:
            fresh = [code for code in codes if code not in self._held]
            if fresh:
                self._ydotool(["key", *[f"{code}:1" for code in fresh]])
            deadline = time.monotonic() + HOLD_TIMEOUT_SECONDS
            for code in codes:
                self._held[code] = deadline

    def _up(self, codes: tuple[int, ...]) -> None:
        if not self.live:
            return
        if not have_ydotool():
            return
        with self._lock:
            for code in codes:
                self._held.pop(code, None)
            self._ydotool(["key", *[f"{code}:0" for code in reversed(codes)]])

    # -- other actions ---------------------------------------------------

    def _execute(self, action: Action) -> None:
        if action.kind == "noop":
            self.log("  (no-op)")
            return
        if action.kind == "key":
            self._tap(action.keys)
            return
        if action.kind == "sequence":
            if not self.live:
                self.log("  (dry run; --inject ydotool to execute)")
                return
            for step in action.sequence:
                self._execute(step)
            return
        if not self.live:
            self.log("  (dry run; --inject ydotool to execute)")
            return

        if action.kind == "text":
            if not have_ydotool():
                self.log("[action] ydotool is not installed")
                return
            with self._lock:
                self._ydotool(
                    ["type", "-d", "10", "-H", "10", "--", action.text], timeout=10.0
                )
        elif action.kind == "command":
            self._run_command(action.command)
        elif action.kind == "desktop":
            self._launch_desktop(action.desktop)
        elif action.kind == "app":
            self._launch_app(action.app_name)
        elif action.kind == "dbus":
            self._call_dbus(*action.dbus)

    def _run_command(self, command: tuple[str, ...]) -> None:
        try:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            self.log(f"[action] cannot run {command[0]!r}: {exc}")
            return

        def wait() -> None:
            _, stderr = process.communicate()
            if process.returncode != 0:
                detail = (stderr or "").strip()
                suffix = f": {detail}" if detail else ""
                self.log(f"[action] {' '.join(command)} exited rc={process.returncode}{suffix}")

        threading.Thread(target=wait, name="command-wait", daemon=True).start()

    def _launch_app(self, name: str) -> None:
        """Use the Fire Remote Client's launch_app for the app row.

        That path handles desktop redirects, kstart5 window activation,
        gtk-launch/xdg-open fallbacks, and fuzzy app names. If the shared
        command core is unavailable, fall back to the simple desktop launcher.
        """
        if self._app_handler is None:
            try:
                scripts = Path(__file__).resolve().parent.parent / "scripts"
                if str(scripts) not in sys.path:
                    sys.path.insert(0, str(scripts))
                import voice_core  # noqa: PLC0415 - lazy, needs the FRC tree

                self._app_handler = voice_core.build_handler()[0]
            except Exception as exc:  # noqa: BLE001 - optional shared core
                self.log(f"[action] FRC app launcher unavailable: {exc}")
                self._app_handler = False
        if self._app_handler:
            try:
                import voice_core  # noqa: PLC0415 - lazy

                if not voice_core._known_app(self._app_handler, name):
                    self.log(
                        f"[action] app {name!r} not found in .desktop files; "
                        "using desktop launcher"
                    )
                    self._launch_desktop(name)
                    return
                result = self._app_handler.launch_app(name)
                self.log(f"[action] app {name!r}: {result}")
                return
            except Exception as exc:  # noqa: BLE001 - handler backends vary
                self.log(f"[action] FRC app launch failed: {exc}")
        self._launch_desktop(name)

    def _desktop_file(self, target: str) -> Path | None:
        """Resolve a .desktop id/path across the standard application dirs."""
        path = Path(target).expanduser()
        if path.is_file():
            return path
        names = [target]
        if not target.endswith(".desktop"):
            names.append(target + ".desktop")
        dirs = [
            Path.home() / ".local" / "share" / "applications",
            Path("/usr/local/share/applications"),
            Path("/usr/share/applications"),
        ]
        for directory in dirs:
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    def _exec_from_desktop(self, desktop: Path) -> None:
        """Last-resort launch by parsing the desktop entry's Exec= line."""
        try:
            content = desktop.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            self.log(f"[action] cannot read {desktop}: {exc}")
            return
        exec_line = ""
        for line in content.splitlines():
            if line.startswith("Exec="):
                exec_line = line.split("=", 1)[1].strip()
                break
        if not exec_line:
            self.log(f"[action] {desktop.name} has no Exec= line")
            return
        parts = [
            part
            for part in shlex.split(exec_line)
            if part not in ("%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k")
        ]
        if not parts:
            return
        try:
            subprocess.Popen(
                parts,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.log(f"[action] launched {desktop.stem} via Exec=")
        except OSError as exc:
            self.log(f"[action] Exec= launch failed: {exc}")

    def _launch_desktop(self, target: str) -> None:
        path = Path(target).expanduser()
        if path.exists():
            self._open_path(path)
            return

        launcher = shutil.which("gtk-launch")
        if launcher:
            try:
                result = subprocess.run(
                    [launcher, target],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(f"[action] gtk-launch failed: {exc}")
            else:
                if result.returncode == 0:
                    self.log(f"[action] launched {target}")
                    return
                self.log(
                    f"[action] gtk-launch {target} rc={result.returncode}: "
                    f"{result.stderr.strip()}; trying other launchers"
                )

        desktop = self._desktop_file(target)
        if desktop is not None:
            gio = shutil.which("gio")
            if gio:
                try:
                    result = subprocess.run(
                        [gio, "launch", str(desktop)],
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                    if result.returncode == 0:
                        self.log(f"[action] launched {target} via gio")
                        return
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.log(f"[action] gio launch failed: {exc}")
            self._exec_from_desktop(desktop)
            return

        self._open_path(target)

    def _open_path(self, target: str | Path) -> None:
        for name in ("xdg-open", "gio", "kde-open5", "kde-open", "gnome-open"):
            opener = shutil.which(name)
            if not opener:
                continue
            argv = [opener]
            if name == "gio":
                argv += ["open", str(target)]
            else:
                argv.append(str(target))
            try:
                subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self.log(f"[action] opened {target} via {name}")
                return
            except OSError as exc:
                self.log(f"[action] {name} failed: {exc}")
        self.log("[action] no opener found (xdg-open/gio/kde-open/gnome-open)")

    def _call_dbus(self, service: str, path: str, interface: str, method: str) -> None:
        busctl = shutil.which("busctl")
        if busctl:
            try:
                result = subprocess.run(
                    [busctl, "--user", "call", service, path, interface, method],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    return
                self.log(
                    f"[action] busctl {method} rc={result.returncode}: "
                    f"{result.stderr.strip()}; trying gdbus"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(f"[action] busctl failed: {exc}; trying gdbus")

        gdbus = shutil.which("gdbus")
        if gdbus:
            try:
                result = subprocess.run(
                    [
                        gdbus, "call", "--session",
                        "--dest", service,
                        "--object-path", path,
                        "--method", f"{interface}.{method}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    return
                self.log(
                    f"[action] gdbus {method} rc={result.returncode}: "
                    f"{result.stderr.strip()}; trying dbus-send"
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(f"[action] gdbus failed: {exc}; trying dbus-send")

        dbus_send = shutil.which("dbus-send")
        if dbus_send:
            try:
                result = subprocess.run(
                    [
                        dbus_send, "--session",
                        f"--dest={service}",
                        "--type=method_call",
                        path,
                        f"{interface}.{method}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode != 0:
                    self.log(
                        f"[action] dbus-send {method} rc={result.returncode}: "
                        f"{result.stderr.strip()}"
                    )
                return
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(f"[action] dbus-send failed: {exc}")
                return
        self.log("[action] no D-Bus tool found (busctl/gdbus/dbus-send)")


# ---------------------------------------------------------------------------
# CLI helpers / self-test

def _self_test() -> int:
    keymap = load_keymap()
    expected = {
        "up": 103,
        "down": 108,
        "left": 105,
        "right": 106,
        "enter": 28,
        "esc": 1,
        "leftmeta": 125,
        "backspace": 14,
        "playpause": 164,
    }
    for name, code in expected.items():
        if KEYCODES[name] != code:
            raise SystemExit(f"FAIL: {name} is {KEYCODES[name]}, expected {code}")

    action = parse_action(keymap["keys"]["select"])
    assert action.kind == "key" and action.keys == (28,), action
    action = parse_action(keymap["keys"]["home"])
    assert action.kind == "key" and action.keys == (172,) and not action.hold, action
    action = parse_action(keymap["keys"]["menu"])
    assert action.kind == "dbus" and action.dbus[3] == "activateLauncherMenu", action
    action = parse_action(keymap["keys"]["power"])
    assert action.kind == "sequence" and len(action.sequence) == 2, action
    action = parse_action("key:leftmeta+w")
    assert action.keys == (125, 17), action
    action = parse_action({"type": "command", "command": ["echo", "hi"]})
    assert action.command == ("echo", "hi"), action
    action = parse_action(app_action_spec(keymap["apps"]["linux.firefox"]))
    assert action.kind == "app" and action.app_name == "Firefox", action

    runner = ActionRunner(keymap, live=False)
    runner.key("dpad_up", "keyDown")
    runner.key("dpad_up", "keyUp")
    runner.key("menu")
    runner.media("scan", {"direction": "backward"})
    runner.text("hi")
    runner.app("linux.firefox")
    runner.close()
    print("PASS: key codes, parsers and dry-run dispatch")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keymap", type=Path, default=None, help="JSON override file")
    parser.add_argument("--print-defaults", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.print_defaults:
        print(json.dumps(DEFAULT_KEYMAP, indent=2))
        return 0
    if args.self_test:
        return _self_test()

    print(json.dumps(load_keymap(args.keymap), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
