#!/usr/bin/env python3
"""User-facing CLI for the Fire TV receiver.

Commands:

    python -m receiver.cli run       start DIAL + HTTPS + WSS (foreground)
    firetv-remote pair            ensure the service is up and show the PIN
    firetv-remote pin             print the most recent pairing PIN
    firetv-remote devices         list paired phones
    firetv-remote forget <name>   forget a phone (or --all)
    firetv-remote doctor          check tools, ports, GUI and STT
    firetv-remote preview-pin     show the PIN card for design/testing
    firetv-remote remap           capture a remote button and map an action
    firetv-remote config init     write the user config
    firetv-remote config show     print the user config

The service unit installed by install.sh runs `python -m receiver.cli run`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import actions, devices, pin_display, remap, server

APP_DIR = "firetv-receiver"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_DIR
CONFIG_PATH = CONFIG_DIR / "config.json"
LAST_PIN_PATH = CONFIG_DIR / "last-pin"

DEFAULT_CONFIG: dict[str, Any] = {
    "name": None,
    "bind": None,
    "ssdp_port": 1900,
    "desc_port": 60000,
    "apps_port": 8009,
    "https_port": 8080,
    "https_bind": None,
    "voice_port": 9090,
    "pin_display": "auto",
    "inject": "ydotool",
    "keymap": None,
    "voice_config": None,
    "cert_dir": None,
    "no_voice": False,
    "verbose": False,
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    target = Path(path).expanduser() if path else CONFIG_PATH
    if not target.exists():
        return cfg
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read {target}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"{target} must contain a JSON object")
    cfg.update(data)
    return cfg


def write_config(cfg: dict[str, Any], path: Path | None = None, force: bool = False) -> Path:
    target = Path(path).expanduser() if path else CONFIG_PATH
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def _fmt_time(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "?"


def _service_active() -> bool | None:
    if not shutil.which("systemctl"):
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "firetv-receiver.service"],
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Commands

def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    argv: list[str] = []
    if cfg.get("name"):
        argv += ["--name", str(cfg["name"])]
    if cfg.get("bind"):
        argv += ["--bind", str(cfg["bind"])]
    if cfg.get("ssdp_port"):
        argv += ["--ssdp-port", str(cfg["ssdp_port"])]
    if cfg.get("desc_port"):
        argv += ["--desc-port", str(cfg["desc_port"])]
    if cfg.get("apps_port"):
        argv += ["--apps-port", str(cfg["apps_port"])]
    if cfg.get("https_port"):
        argv += ["--https-port", str(cfg["https_port"])]
    if cfg.get("https_bind"):
        argv += ["--https-bind", str(cfg["https_bind"])]
    if cfg.get("voice_port"):
        argv += ["--voice-port", str(cfg["voice_port"])]
    if cfg.get("pin_display"):
        argv += ["--pin-display", str(cfg["pin_display"])]
    if cfg.get("inject"):
        argv += ["--inject", str(cfg["inject"])]
    if cfg.get("keymap"):
        argv += ["--keymap", str(cfg["keymap"])]
    if cfg.get("cert_dir"):
        argv += ["--cert-dir", str(cfg["cert_dir"])]
    if cfg.get("voice_config"):
        argv += ["--voice-config", str(cfg["voice_config"])]
    if cfg.get("no_voice"):
        argv.append("--no-voice")
    if args.verbose or cfg.get("verbose"):
        argv.append("--verbose")
    return server.main(argv)


def cmd_pair(args: argparse.Namespace) -> int:
    active = _service_active()
    if active is False:
        if shutil.which("systemctl"):
            result = subprocess.run(
                ["systemctl", "--user", "start", "firetv-receiver.service"],
                timeout=6,
                check=False,
            )
            if result.returncode != 0:
                print("could not start firetv-receiver.service; run `firetv-remote run`.")
        else:
            print("systemd is unavailable; run `firetv-remote run` in a terminal.")
    cfg = load_config(args.config)
    name = cfg.get("name") or socket.gethostname().split(".", 1)[0]
    print(f'In the official Amazon Fire TV app, select "{name}".')
    print("The 4-digit PIN card appears in the top-right; the PIN is also printed")
    print("by `firetv-remote pin` and in `journalctl --user -u firetv-receiver -f`.")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    if not LAST_PIN_PATH.exists():
        print("No PIN has been requested yet.")
        return 1
    try:
        data = json.loads(LAST_PIN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {LAST_PIN_PATH}: {exc}", file=sys.stderr)
        return 1
    pin = str(data.get("pin") or "")
    created = float(data.get("created") or 0)
    age = max(0, int(time.time() - created))
    expired = age > server.PIN_TTL_SECONDS
    print(f"PIN: {pin}   (created {_fmt_time(created)}, {age}s ago"
          f"{', expired' if expired else ''})")
    if expired:
        print("The app must request a new PIN; select the device again.")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    store = devices.DeviceStore()
    items = store.list()
    if args.json:
        print(json.dumps([d.to_json() for d in items], indent=2))
        return 0
    if not items:
        print("No paired phones.")
        return 0
    print(f"{'#':>2}  {'name':<24} {'ip':<16} {'paired':<16} {'last seen':<16} token")
    for index, device in enumerate(items, start=1):
        print(
            f"{index:>2}  {device.name[:24]:<24} "
            f"{(device.ip or '-'):<16} {_fmt_time(device.paired_at):<16} "
            f"{_fmt_time(device.last_seen):<16} {device.token_prefix}"
        )
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    store = devices.DeviceStore()
    if args.all:
        count = store.clear()
        print(f"Forgot {count} device(s).")
        return 0
    if not args.query:
        print("give a device name, token prefix or index, or use --all", file=sys.stderr)
        return 2
    try:
        count = store.forget(args.query)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if count:
        print(f"Forgot {count} device(s).")
        return 0
    print(f"No device matched {args.query!r}.", file=sys.stderr)
    return 1


def cmd_preview_pin(args: argparse.Namespace) -> int:
    card = pin_display.PinDisplay(enabled=True)
    if not card.available():
        print("PySide6/QML or a graphical session is not available.", file=sys.stderr)
        return 1
    print(f"Showing PIN {args.pin} for {args.ttl}s...")
    card.show(args.pin, args.ttl)
    try:
        time.sleep(args.ttl)
    finally:
        card.close()
    return 0


def _check_port(port: int, udp: bool = False) -> str:
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    sock = socket.socket(socket.AF_INET, kind)
    try:
        if not udp:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
    except OSError as exc:
        return f"busy ({exc.strerror or exc})"
    finally:
        sock.close()
    return "free"


def _has_module(python: str | None, module: str) -> bool:
    if not python:
        return False
    try:
        result = subprocess.run(
            [python, "-c", f"import {module}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print("Fire TV receiver doctor")
    print(f"  config:       {args.config or CONFIG_PATH}")
    print(f"  name:         {cfg.get('name') or socket.gethostname().split('.', 1)[0]}")
    print(f"  inject:       {cfg.get('inject')}")
    print(f"  PIN display:  {cfg.get('pin_display')}")

    tools = {
        "ydotool": shutil.which("ydotool"),
        "cec-policy": shutil.which("cec-policy"),
        "busctl": shutil.which("busctl"),
        "gtk-launch": shutil.which("gtk-launch"),
        "notify-send": shutil.which("notify-send"),
        "systemctl": shutil.which("systemctl"),
    }
    for name, path in tools.items():
        print(f"  {name:<12} {'ok: ' + path if path else 'missing'}")
    if shutil.which("systemctl"):
        try:
            ydotoold = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "ydotoold.service"],
                timeout=4,
                check=False,
            )
            active = ydotoold.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            active = False
        print(f"  ydotoold:     {'active' if active else 'inactive'}")

    gui = pin_display.PinDisplay(enabled=True)
    print(f"  Qt PIN card:  {'available' if gui.available() else 'unavailable'}")
    print(f"  layer-shell:  {'yes' if pin_display.layer_shell_available() else 'no'}")
    whisper = "yes" if _has_module(sys.executable, "faster_whisper") else "no"
    print(f"  faster-whisper in this Python: {whisper}")

    ports = [(1900, True), (60000, False), (8009, False), (8080, False), (9090, False)]
    active = _service_active()
    for port, udp in ports:
        state = _check_port(port, udp)
        if active and state.startswith("busy"):
            state = "in use (service active)"
        print(f"  {'udp' if udp else 'tcp'}/{port}: {state}")

    store = devices.DeviceStore()
    print(f"  paired phones: {len(store.list())} ({store.path})")
    service = "active" if active else "inactive" if active is False else "systemd unavailable"
    print(f"  service:      {service}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    active = _service_active()
    service = "active" if active else "inactive" if active is False else "systemd unavailable"
    print(f"service:  {service}")
    print(f"name:     {cfg.get('name') or socket.gethostname().split('.', 1)[0]}")
    print(f"config:   {args.config or CONFIG_PATH}")
    store = devices.DeviceStore()
    items = store.list()
    print(f"devices:  {len(items)}")
    for device in items:
        seen = _fmt_time(device.last_seen)
        print(f"          {device.name}  ({device.ip or 'no ip'})  last seen {seen}")
    if LAST_PIN_PATH.exists():
        try:
            data = json.loads(LAST_PIN_PATH.read_text(encoding="utf-8"))
            age = int(time.time() - float(data.get("created") or 0))
            print(f"last PIN: {data.get('pin')}  ({age}s ago)")
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    cfg = dict(DEFAULT_CONFIG)
    if args.name is not None:
        cfg["name"] = args.name
    if args.bind is not None:
        cfg["bind"] = args.bind
    if args.pin_display is not None:
        cfg["pin_display"] = args.pin_display
    if args.inject is not None:
        cfg["inject"] = args.inject
    target = write_config(cfg, args.config, force=args.force)
    print(f"Wrote {target}")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    print(json.dumps(load_config(args.config), indent=2))
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    print(json.dumps(actions.load_keymap(args.keymap), indent=2))
    return 0


def _remap_restart(no_restart: bool) -> None:
    if no_restart:
        print("Restart when ready: systemctl --user restart remote-launch.service")
        return
    ok, message = remap.restart_service()
    if ok:
        print("Restarted remote-launch.service")
    else:
        print(
            "Could not restart remote-launch.service"
            f"{': ' + message if message else ''}.\n"
            "Run: systemctl --user restart remote-launch.service"
        )


def cmd_remap(args: argparse.Namespace) -> int:
    config_path = Path(args.file).expanduser() if args.file else remap.DEFAULT_CONFIG

    if args.list_keys:
        for name, code in sorted(remap.KEY_NAMES.items(), key=lambda item: item[1]):
            print(f"{name}: {code}")
        return 0

    if args.list_desktops:
        entries = remap.list_desktops()
        if not entries:
            print("No .desktop entries found.")
            return 0
        for index, entry in enumerate(entries, start=1):
            print(f"{index:3}. {entry['name']}  ({entry['file']})")
        return 0

    try:
        cfg = remap.load_config(config_path)
    except remap.RemapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.list_mappings:
        remap.list_mappings(cfg)
        return 0

    if args.reset:
        remap.reset_config(cfg)
        path = remap.save_config(cfg, config_path)
        print(f"Cleared all button mappings in {path}")
        _remap_restart(args.no_restart)
        return 0

    if args.remove:
        removed = remap.remove_mapping(cfg, args.remove)
        if not removed:
            print(f"No mapping matched {args.remove!r}")
            return 1
        path = remap.save_config(cfg, config_path)
        print(f"Removed {removed} mapping(s) from {path}")
        _remap_restart(args.no_restart)
        return 0

    if args.action and args.desktop:
        print("error: use --action/--command or --desktop, not both", file=sys.stderr)
        return 2

    action = args.action
    if args.desktop:
        try:
            action = remap.resolve_desktop(args.desktop)
        except remap.RemapError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    key_spec = args.button
    scan_spec = args.scan
    if args.capture or not key_spec:
        print("Press a button on the Fire TV remote (Ctrl-C to cancel)...", flush=True)
        try:
            captured = remap.capture_button(args.device, args.timeout)
        except remap.RemapError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            return 130
        key_spec = captured.key_name
        if (
            scan_spec is None
            and captured.scan is not None
            and captured.key == remap.KEY_PROGRAM
        ):
            scan_spec = f"0x{captured.scan:x}"
        scan_text = f" scan=0x{captured.scan:x}" if captured.scan is not None else ""
        print(f"Captured {captured.key_name} (key {captured.key}{scan_text})")

    try:
        if remap.parse_key(key_spec) == remap.KEY_PROGRAM and not scan_spec:
            print(
                "warning: KEY_PROGRAM without --scan matches any app-shortcut "
                "button. Re-run with --scan to distinguish them.",
                file=sys.stderr,
            )
    except remap.RemapError:
        pass

    if action is None:
        if not sys.stdin.isatty():
            print("error: pass --action/--command or run interactively", file=sys.stderr)
            return 2
        action = input("Action (shell command, .desktop path, or file): ").strip()
    if not action:
        print("error: action cannot be empty", file=sys.stderr)
        return 2

    try:
        entry, created = remap.add_mapping(
            cfg,
            key=key_spec,
            action=action,
            scan=scan_spec,
            name=args.name,
        )
    except remap.RemapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    path = remap.save_config(cfg, config_path)
    verb = "Added" if created else "Updated"
    print(f"{verb} mapping {entry['name']!r} -> {entry['action']}")
    print(f"Saved {path}")
    _remap_restart(args.no_restart)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="firetv-remote",
        description="Official Amazon Fire TV app receiver for Linux",
    )
    parser.add_argument("--config", type=Path, default=None, help="config JSON path")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start DIAL + HTTPS + WSS in the foreground")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    pair = sub.add_parser("pair", help="ensure the service is running and pair")
    pair.set_defaults(func=cmd_pair)

    pin = sub.add_parser("pin", help="print the most recent pairing PIN")
    pin.set_defaults(func=cmd_pin)

    dev = sub.add_parser("devices", help="list paired phones")
    dev.add_argument("--json", action="store_true")
    dev.set_defaults(func=cmd_devices)

    status = sub.add_parser("status", help="service, devices and last PIN")
    status.set_defaults(func=cmd_status)

    forget = sub.add_parser("forget", help="forget a paired phone")
    forget.add_argument("query", nargs="?", help="name, token prefix or index")
    forget.add_argument("--all", action="store_true")
    forget.set_defaults(func=cmd_forget)

    preview = sub.add_parser("preview-pin", help="show the PIN card for testing")
    preview.add_argument("--pin", default="8421")
    preview.add_argument("--ttl", type=int, default=8)
    preview.set_defaults(func=cmd_preview_pin)

    doctor = sub.add_parser("doctor", help="check tools, ports, GUI and STT")
    doctor.set_defaults(func=cmd_doctor)

    keys = sub.add_parser("keys", help="print the effective action keymap")
    keys.add_argument("--keymap", type=Path, default=None)
    keys.set_defaults(func=cmd_keys)

    remap_p = sub.add_parser(
        "remap", help="capture a remote button and map it to an action"
    )
    remap_p.add_argument(
        "--capture", action="store_true", help="capture the next remote button press"
    )
    remap_p.add_argument(
        "--button", "--key", dest="button", default=None,
        help="evdev key name or code (default: capture)",
    )
    remap_p.add_argument(
        "--scan", default=None, help="MSC_SCAN value, decimal or 0x hex"
    )
    remap_p.add_argument(
        "--action", "--command", dest="action", default=None,
        help="shell command, .desktop path, or file/directory to run",
    )
    remap_p.add_argument(
        "--desktop", default=None, help="desktop entry number/name/path"
    )
    remap_p.add_argument("--name", default=None, help="mapping name")
    remap_p.add_argument("--remove", default=None, help="remove by name, key or scan")
    remap_p.add_argument("--reset", action="store_true", help="remove all mappings")
    remap_p.add_argument(
        "--list", dest="list_mappings", action="store_true",
        help="list current mappings",
    )
    remap_p.add_argument(
        "--list-keys", action="store_true", help="list known evdev key names"
    )
    remap_p.add_argument(
        "--list-desktops", action="store_true", help="list .desktop entries"
    )
    remap_p.add_argument(
        "--file", default=None,
        help="button YAML (default: config/button_launch.yaml)",
    )
    remap_p.add_argument(
        "--device", default=None, help="evdev path (default: auto-detect)"
    )
    remap_p.add_argument(
        "--timeout", type=float, default=15.0, help="capture timeout in seconds"
    )
    remap_p.add_argument(
        "--no-restart", action="store_true",
        help="do not restart remote-launch.service after saving",
    )
    remap_p.set_defaults(func=cmd_remap)

    config = sub.add_parser("config", help="inspect or create the config")
    config_sub = config.add_subparsers(dest="config_command")
    init = config_sub.add_parser("init", help="write the config file")
    init.add_argument("--name")
    init.add_argument("--bind")
    init.add_argument("--pin-display", choices=("auto", "qt", "none"))
    init.add_argument("--inject", choices=("log", "ydotool"))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_config_init)
    show = config_sub.add_parser("show", help="print the config")
    show.set_defaults(func=cmd_config_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if args.command == "config" and not getattr(args, "config_command", None):
        parser.parse_args(["config", "--help"])
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
