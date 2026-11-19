#!/usr/bin/env python3
"""Fire TV DIAL + Turnstile HTTPS pairing PoC.

This is the second experiment after `dial.py`:

1. advertises a DIAL/Fire TV device so the official app lists it;
2. serves the Turnstile API on HTTPS 8080 with a self-signed certificate;
3. shows the 4-digit PIN in a red Qt card and in this terminal;
4. accepts pairing and maps keys/media/text to desktop actions.

The mapping lives in ``receiver/actions.py`` and is data-driven; see
``config/keymap.example.json``. By default actions are logged only (dry run).
Add ``--inject ydotool`` to actually press keys, type text, run CEC commands
and launch apps.

For the installed experience use ``receiver/cli.py`` (``firetv-receiver``):
``install.sh`` creates the launcher, config, device store and systemd user
service. This script remains the foreground/development entry point.

It intentionally uses a certificate generated on this Linux box. The
question this experiment answers is whether the official phone app trusts
that certificate. If the phone app rejects it, the HTTPS server prints a
TLS handshake error and the app shows a connection error.

Run:

    python3 python -m receiver.cli run --inject ydotool

Show the effective mapping without starting anything:

    python3 python -m receiver.cli run --print-keymap

No system files are modified. The certificate and stable device UUID live
under ~/.config/firetv-receiver/poc-tls/ unless --cert-dir is given.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import actions as action_map
from . import devices as device_store
from . import dial, icons, voice
from . import pin_display as pin_card

API_KEY = "0987654321"
PIN_TTL_SECONDS = 60
MAX_PIN_ATTEMPTS = 5

STATE_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "firetv-receiver"
)
LAST_PIN_PATH = STATE_DIR / "last-pin"


# ---------------------------------------------------------------------------
# Pairing state

def record_last_pin(pin: str) -> None:
    """Keep the most recent PIN for `firetv-receiver pin` and headless setups."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_PIN_PATH.write_text(
            json.dumps({"pin": pin, "created": time.time()}) + "\n",
            encoding="utf-8",
        )
        LAST_PIN_PATH.chmod(0o600)
    except OSError as exc:
        print(f"[pair] could not write {LAST_PIN_PATH}: {exc}", flush=True)


@dataclass
class PairState:
    pin: str | None = None
    pin_expiry: float = 0.0
    pin_attempts: int = 0
    friendly: str = ""
    store: device_store.DeviceStore | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def load(self, store: device_store.DeviceStore) -> None:
        self.store = store
        print(
            f"[pair] loaded {len(store.list())} saved device(s) from {store.path}",
            flush=True,
        )

    def new_pin(self, friendly: str) -> str:
        with self.lock:
            self.pin = f"{secrets.randbelow(10000):04d}"
            self.pin_expiry = time.time() + PIN_TTL_SECONDS
            self.pin_attempts = 0
            self.friendly = friendly
            record_last_pin(self.pin)
            return self.pin

    def verify(self, pin: str, ip: str = "") -> tuple[str, str | None]:
        with self.lock:
            if pin == "-1":
                self.pin = None
                return "cancel", None
            if not self.pin or time.time() > self.pin_expiry:
                self.pin = None
                return "expired", None
            if self.pin_attempts >= MAX_PIN_ATTEMPTS:
                return "throttle", None
            if not secrets.compare_digest(pin, self.pin):
                self.pin_attempts += 1
                if self.pin_attempts >= MAX_PIN_ATTEMPTS:
                    return "throttle", None
                return "wrong", None
            token = (
                self.store.add(self.friendly, ip)
                if self.store is not None
                else secrets.token_urlsafe(24)
            )
            self.pin = None
            return "ok", token

    def valid(self, token: str | None) -> bool:
        return bool(self.store and self.store.valid(token))

    def clear(self) -> None:
        with self.lock:
            self.pin = None
            self.pin_attempts = 0
            if self.store is not None:
                self.store.clear()
            self._save_locked()


STATE = PairState()


# ---------------------------------------------------------------------------
# Input mapping / action execution

# Mapping and execution live in receiver/actions.py so the same defaults can be
# shared with fire-remote-client later. See config/keymap.example.json.


# ---------------------------------------------------------------------------
# Certificate helpers

def certificate_fingerprint(cert_path: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def ensure_certificate(cert_dir: Path, bind_ip: str, name: str) -> tuple[Path, Path]:
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    if shutil.which("openssl") is None:
        raise SystemExit(
            "openssl is required to generate the PoC certificate.\n"
            "Fedora: sudo dnf install openssl\n"
            "Debian/Ubuntu: sudo apt install openssl"
        )

    hostname = socket.gethostname().split(".", 1)[0] or "firetv-receiver"
    cn = re.sub(r"[^A-Za-z0-9 ._-]", "", name).strip() or hostname
    san = f"DNS:{hostname},DNS:{hostname}.local,IP:{bind_ip}"
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
        "-days", "365",
        "-keyout", str(key_path),
        "-out", str(cert_path),
        "-subj", f"/CN={cn}",
        "-addext", f"subjectAltName={san}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(
            "openssl could not generate the certificate:\n"
            f"{result.stderr.strip()}\n"
            "If this is an older OpenSSL without -addext, remove that option and "
            "regenerate; the phone app may still accept the certificate."
        )
    key_path.chmod(0o600)
    print(f"[tls] generated self-signed certificate for {cn} ({san})", flush=True)
    return cert_path, key_path


def load_or_create_udn(cert_dir: Path) -> str:
    cert_dir.mkdir(parents=True, exist_ok=True)
    udn_file = cert_dir / "udn.txt"
    if udn_file.exists():
        value = udn_file.read_text(encoding="ascii").strip()
        if value.startswith("uuid:"):
            return value
    value = f"uuid:{uuid.uuid4()}"
    udn_file.write_text(value + "\n", encoding="ascii")
    return value


def build_app_assets(
    keymap: dict[str, dict[str, Any]], cfg: dial.Config
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Build the app-row JSON and the icon files served over DIAL HTTP."""
    entries = action_map.advertised_apps(keymap)
    apps: list[dict[str, Any]] = []
    icon_files: dict[str, bytes] = {}

    for entry in entries:
        app_id = str(entry["appId"])
        name = str(entry["name"])
        action = entry["action"]
        icon_spec = entry.get("icon")
        external = isinstance(icon_spec, str) and icon_spec.startswith(
            ("http://", "https://")
        )
        if external:
            icon_url = str(icon_spec)
        else:
            desktop_id = icons.desktop_id_from_action(
                action.kind, action.desktop or action.app_name
            )
            icon_path = icons.resolve_icon(icon_spec, desktop_id, app_id)
            filename = icons.icon_filename(app_id)
            icon_files[filename] = icons.load_icon_bytes(icon_path, name)
            icon_url = f"{cfg.application_url}icon/{filename}"

        apps.append(
            {
                "appId": app_id,
                "name": name,
                "isInstalled": True,
                "isShortcutApp": False,
                "tvIconArt": icon_url,
                "iconArtSmallUri": icon_url,
                "appShortcutLaunchIntent": "",
            }
        )
    return apps, icon_files


# ---------------------------------------------------------------------------
# HTTPS server

class TurnstileServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ssl.SSLError):
            print(
                f"[tls] handshake/connection error from {client_address}: {exc}",
                flush=True,
            )
        elif isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            print(f"[tls] client disconnected: {client_address}", flush=True)
        else:
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TurnstilePoC/0.1"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    # -- plumbing ---------------------------------------------------------

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _send(self, code: int, value: Any = None) -> None:
        if value is None:
            body = b""
        else:
            body = json.dumps(value).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _api_key_ok(self) -> bool:
        return self.headers.get("x-api-key") == API_KEY

    def _token_ok(self) -> bool:
        return STATE.valid(self.headers.get("x-client-token"))

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body = self._body() if method == "POST" else {}

        if getattr(self.server, "verbose", False):
            print(
                f"[req] {method} {self.path} token={bool(self.headers.get('x-client-token'))} "
                f"body={body}",
                flush=True,
            )

        if not self._api_key_ok():
            self._send(403, {"description": "bad api key"})
            return

        pin_route = path in ("/v1/FireTV/pin/display", "/v1/FireTV/pin/verify")
        if not pin_route and not self._token_ok():
            self._send(403, {"description": "Request unauthorized, missing client token"})
            return
        if not pin_route and STATE.store is not None:
            STATE.store.touch(self.headers.get("x-client-token"), self.client_address[0])

        # ---- pairing ---------------------------------------------------
        if path == "/v1/FireTV/pin/display" and method == "POST":
            friendly = str(body.get("friendlyName") or "phone")
            pin = STATE.new_pin(friendly)
            print()
            print("=" * 62)
            print(f" PAIRING REQUEST from {friendly}")
            print(f" PIN: {pin}   (valid {PIN_TTL_SECONDS}s)")
            print("=" * 62)
            print(flush=True)
            pin_display = getattr(self.server, "pin_display", None)
            if pin_display is not None:
                shown = pin_display.show(pin, PIN_TTL_SECONDS)
                print(
                    f"[pair] PIN card {'shown' if shown else 'unavailable; see terminal'}",
                    flush=True,
                )
            self._send(200, {"description": "OK"})
            return

        if path == "/v1/FireTV/pin/verify" and method == "POST":
            pin = str(body.get("pin", ""))
            outcome, token = STATE.verify(pin, self.client_address[0])
            pin_display = getattr(self.server, "pin_display", None)
            if outcome == "ok":
                if pin_display is not None:
                    pin_display.clear()
                print(f"[pair] PAIRED with {STATE.friendly}; token={token[:10]}...", flush=True)
                self._send(200, {"description": token})
            elif outcome == "throttle":
                if pin_display is not None:
                    pin_display.clear()
                print("[pair] too many wrong PINs; throttled", flush=True)
                self._send(429, {"description": "too many attempts"})
            elif outcome == "wrong":
                print("[pair] wrong PIN", flush=True)
                self._send(200, {"description": ""})
            elif outcome == "cancel":
                if pin_display is not None:
                    pin_display.clear()
                print("[pair] pairing cancelled", flush=True)
                self._send(200, {"description": ""})
            else:
                if pin_display is not None:
                    pin_display.clear()
                print("[pair] PIN expired or no PIN requested", flush=True)
                self._send(200, {"description": ""})
            return

        # ---- status / device info --------------------------------------
        if path == "/v1/FireTV/status" and method == "GET":
            # Empty turnstileVersion selects the app's v1 endpoint family.
            self._send(
                200,
                {
                    "turnstileVersion": "",
                    "osVersion": "Linux",
                    "platformType": "android",
                },
            )
            return

        if path in ("/v1/FireTV", "/v1/FireTV2") and method == "GET":
            self._send(
                200,
                {
                    "isVolumeControlsSupported": True,
                    "volumeCapabilityLevelOfSupport": "Supported",
                    "powerCapabilityLevelOfSupport": "Supported",
                    "ringableRemoteCount": 0,
                    "isEpgSupported": False,
                    "isInternalIntentSupported": False,
                    "isPropertiesApiSupported": True,
                },
            )
            return

        if path == "/v1/FireTV/properties" and method == "GET":
            self._send(
                200,
                {
                    "osVersion": "Linux",
                    "platformType": "android",
                    "turnstileVersion": "",
                    "volumeSupport": "Supported",
                    "muteSupport": "Supported",
                    "powerSupport": "Supported",
                    "ringMySupport": "NotSupported",
                    "epgSupport": "NotSupported",
                    "internalIntentSupport": "NotSupported",
                    "pfm": "",
                    "dsn": "",
                    "deviceType": "AFTMA08C15",
                    "clearTokensSupport": "Supported",
                    "areYACOptionsAvailable": False,
                    "currentFireTvProfileId": None,
                },
            )
            return

        # Capability feedback sent after the phone's TV-setup prompts.
        if path == "/v1/FireTV/properties" and method == "POST":
            print(f"[capability] {body}", flush=True)
            self._send(200, {"description": "OK"})
            return

        # ---- apps ------------------------------------------------------
        if path in ("/v1/FireTV/apps", "/v1/FireTV/appsV2") and method == "GET":
            self._send(200, getattr(self.server, "apps_list", []))
            return

        if path.startswith("/v1/FireTV/app/") and method == "POST":
            app_id = path.removeprefix("/v1/FireTV/app/")
            print(f"[app] launch requested: {app_id!r}", flush=True)
            self.server.runner.app(app_id)  # type: ignore[attr-defined]
            self._send(200, {"description": "OK"})
            return

        if path.startswith("/v1/FireTV/shortcut/") and method == "POST":
            print(f"[app] shortcut requested: {path!r}", flush=True)
            self._send(200, {"description": "OK"})
            return

        if path == "/v1/FireTV/intent" and method == "POST":
            print(f"[app] intent: {body}", flush=True)
            self._send(200, {"description": "OK"})
            return

        # ---- keys ------------------------------------------------------
        if path == "/v1/FireTV" and method == "POST":
            action = (query.get("action") or [""])[0]
            key_action = body.get("keyActionType")
            print(
                f"[key] action={action!r} keyActionType={key_action!r} body={body}",
                flush=True,
            )
            self.server.runner.key(action, key_action)  # type: ignore[attr-defined]
            self._send(200, {"description": "OK"})
            return

        # ---- media -----------------------------------------------------
        if path == "/v1/media" and method == "POST":
            action = (query.get("action") or [""])[0]
            print(f"[media] action={action!r} body={body}", flush=True)
            self.server.runner.media(action, body)  # type: ignore[attr-defined]
            self._send(200, {"description": "OK"})
            return

        # ---- keyboard / text ------------------------------------------
        if path == "/v1/FireTV/keyboard" and method == "GET":
            self._send(200, {"state": "hidden", "mode": None, "text": None})
            return

        if path == "/v1/FireTV/keyboard" and method == "POST":
            action = (query.get("action") or [""])[0]
            text = str(body.get("text", ""))
            print(f"[text] action={action!r} {text!r}", flush=True)
            if action == "backspace":
                self.server.runner.key("backspace")  # type: ignore[attr-defined]
            else:
                self.server.runner.text(text)  # type: ignore[attr-defined]
            self._send(200, {"description": "OK"})
            return

        if path == "/v1/FireTV/text" and method == "POST":
            action = (query.get("action") or [""])[0]
            text = str(body.get("text", ""))
            print(f"[text] action={action!r} {text!r} (legacy)", flush=True)
            if action == "backspace":
                self.server.runner.key("backspace")  # type: ignore[attr-defined]
            else:
                self.server.runner.text(text)  # type: ignore[attr-defined]
            self._send(200, {"description": "OK"})
            return

        # ---- voice / housekeeping -------------------------------------
        if path == "/v1/FireTV/voiceCommand" and method == "POST":
            action = (query.get("action") or [""])[0]
            print(f"[voice] HTTP action={action!r}", flush=True)
            self._send(200, {"description": "OK"})
            return

        if path == "/v1/FireTV/clearTokens" and method == "POST":
            STATE.clear()
            self.server.runner.release_all()  # type: ignore[attr-defined]
            print("[pair] all tokens cleared", flush=True)
            self._send(200, {"description": "OK"})
            return

        if path == "/v1/FireTV/ringRemotes" and method == "POST":
            print("[ring] find-my-remote requested", flush=True)
            self._send(200, {"description": "OK"})
            return

        self._send(404, {"description": "not found"})


# ---------------------------------------------------------------------------
# Main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fire TV DIAL + Turnstile HTTPS pairing PoC",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--name",
        default=None,
        help="name shown in the app; defaults to the OS hostname, then 'Linux TV'",
    )
    parser.add_argument("--model", default="AFTMA08C15", help="Fire TV model string")
    parser.add_argument("--bind", default=None, help="LAN IPv4 address to advertise")
    parser.add_argument("--ssdp-port", type=int, default=dial.SSDP_PORT)
    parser.add_argument("--desc-port", type=int, default=dial.DESC_PORT)
    parser.add_argument("--apps-port", type=int, default=dial.APPS_PORT)
    parser.add_argument(
        "--https-bind",
        default=None,
        help="address to bind HTTPS (default: the --bind LAN address, not 0.0.0.0)",
    )
    parser.add_argument("--https-port", type=int, default=8080)
    parser.add_argument(
        "--cert-dir",
        type=Path,
        default=Path.home() / ".config" / "firetv-receiver" / "poc-tls",
        help="where to store cert.pem, key.pem and the stable UDN",
    )
    parser.add_argument(
        "--inject",
        choices=("log", "ydotool"),
        default="log",
        help=(
            "log = show the mapped action only (dry run); "
            "ydotool = execute keys/text with ydotool, run commands and "
            "launch apps"
        ),
    )
    parser.add_argument(
        "--keymap",
        type=Path,
        default=None,
        help=(
            "JSON mapping override; defaults to config/keymap.json when that "
            "file exists, otherwise the built-in map"
        ),
    )
    parser.add_argument(
        "--print-keymap",
        action="store_true",
        help="print the effective mapping as JSON and exit",
    )
    parser.add_argument(
        "--pin-display",
        choices=("auto", "qt", "none"),
        default="auto",
        help="pairing PIN presentation: auto (Qt card if available), qt, or none",
    )
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="do not start the WSS 9090 voice receiver",
    )
    parser.add_argument(
        "--voice-config",
        type=Path,
        default=None,
        help="voice JSON config; defaults to config/voice.json when present",
    )
    parser.add_argument(
        "--voice-port",
        type=int,
        default=None,
        help="WSS voice port (the official app hardcodes 9090)",
    )
    parser.add_argument(
        "--voice-type",
        action="store_true",
        help="type the transcript into the focused window",
    )
    parser.add_argument(
        "--no-voice-stt",
        action="store_true",
        help="capture WAV only; do not run faster-whisper",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    keymap = action_map.load_keymap(args.keymap)
    if args.print_keymap:
        print(json.dumps(keymap, indent=2))
        return 0

    STATE.load(device_store.DeviceStore())

    voice_cfg = voice.load_voice_config(args.voice_config)
    if args.no_voice:
        voice_cfg["enabled"] = False
    if args.no_voice_stt:
        voice_cfg["stt"] = False
    if args.voice_type:
        voice_cfg["type_text"] = True
    if args.voice_port is not None:
        voice_cfg["port"] = args.voice_port

    if args.inject == "ydotool" and not action_map.have_ydotool():
        print(
            "warning: --inject ydotool requested but ydotool is not in PATH; "
            "falling back to log-only.",
            file=sys.stderr,
        )
        args.inject = "log"

    if args.bind:
        bind_ip = args.bind
    else:
        # At login the user service can start before Wi-Fi, and Docker/VPN
        # bridges are up first. Wait for a real LAN address rather than
        # binding HTTPS to loopback, which the phone can never reach.
        bind_ip = dial.wait_for_lan_ip(30.0)
        if not bind_ip:
            print(
                "error: no usable LAN IPv4 address after 30s; the phone cannot "
                "reach this host.\n"
                "Connect Wi-Fi/Ethernet and systemd will restart the service "
                "(or set --bind to a fixed address).",
                file=sys.stderr,
            )
            return 1
        if bind_ip.startswith(("127.", "169.254.")) or bind_ip == "0.0.0.0":
            print(
                f"error: refusing to advertise non-LAN address {bind_ip}; "
                "pass --bind with the address the phone should use.",
                file=sys.stderr,
            )
            return 1
    name = args.name or dial.default_device_name()
    cert_path, key_path = ensure_certificate(args.cert_dir, bind_ip, name)
    udn = load_or_create_udn(args.cert_dir)

    dial_cfg = dial.Config(
        name=name,
        model=args.model,
        udn=udn,
        bind_ip=bind_ip,
        ssdp_port=args.ssdp_port,
        desc_port=args.desc_port,
        apps_port=args.apps_port,
        verbose=args.verbose,
    )
    apps_list, icon_files = build_app_assets(keymap, dial_cfg)

    try:
        desc_server, apps_server = dial.start_servers(dial_cfg)
    except OSError as exc:
        print(
            f"error: could not bind DIAL HTTP ports {args.desc_port}/{args.apps_port}: {exc}\n"
            "Check for another process using those ports.",
            file=sys.stderr,
        )
        return 1
    apps_server.icons = icon_files  # type: ignore[attr-defined]

    try:
        ssdp = dial.SSDPResponder(dial_cfg)
    except OSError as exc:
        dial.stop_servers(desc_server, apps_server)
        print(
            f"error: could not bind SSDP port {args.ssdp_port}: {exc}\n"
            "Check for another process using UDP 1900.",
            file=sys.stderr,
        )
        return 1
    ssdp.start()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    # Bind the LAN address, not 0.0.0.0. This coexists with services such as
    # Steam's steamwebhelper that listen only on 127.0.0.1:8080, while still
    # accepting the phone's connection to https://<bind_ip>:8080.
    https_bind = args.https_bind or bind_ip
    try:
        https_server = TurnstileServer((https_bind, args.https_port), Handler)
    except OSError as exc:
        ssdp.close()
        dial.stop_servers(desc_server, apps_server)
        print(
            f"error: could not bind HTTPS on {https_bind}:{args.https_port}: {exc}\n"
            f"Another process is using {https_bind}:{args.https_port}. "
            "Check `ss -ltnp 'sport = :8080'`.\n"
            "The official app always connects to port 8080, so do not change "
            "--https-port; free the address instead.",
            file=sys.stderr,
        )
        return 1
    https_server.socket = context.wrap_socket(https_server.socket, server_side=True)
    runner = action_map.ActionRunner(keymap, live=args.inject == "ydotool")
    pin_display = pin_card.PinDisplay(enabled=args.pin_display != "none")
    if args.pin_display == "qt" and not pin_display.available():
        print(
            "warning: --pin-display qt requested, but PySide6/QML is not "
            "available; the PIN will be printed in this terminal.",
            file=sys.stderr,
        )
    https_server.cfg = dial_cfg  # type: ignore[attr-defined]
    https_server.verbose = args.verbose  # type: ignore[attr-defined]
    https_server.runner = runner  # type: ignore[attr-defined]
    https_server.apps_list = apps_list  # type: ignore[attr-defined]
    https_server.pin_display = pin_display  # type: ignore[attr-defined]
    threading.Thread(target=https_server.serve_forever, daemon=True, name="https").start()

    voice_server: voice.VoiceServer | None = None
    voice_external = False
    if voice_cfg.get("enabled", True):
        try:
            voice_server = voice.VoiceServer(
                bind_ip=bind_ip,
                cert_path=cert_path,
                key_path=key_path,
                cfg=voice_cfg,
                api_key=API_KEY,
                token_ok=STATE.valid,
            )
            voice_server.start()
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                voice_external = True
                print(
                    f"note: voice port {voice_cfg.get('port', 9090)} is in use; "
                    "assuming voice-stt.service owns WSS 9090 (shared model).",
                    file=sys.stderr,
                )
            else:
                print(
                    f"warning: could not bind voice WSS on {bind_ip}:"
                    f"{voice_cfg.get('port', 9090)}: {exc}\n"
                    "         Voice is disabled. Check `ss -ltnp 'sport = :9090'`; "
                    "the app hardcodes 9090.",
                    file=sys.stderr,
                )
            voice_server = None

    if args.keymap is not None:
        keymap_source = str(args.keymap)
    elif action_map.DEFAULT_KEYMAP_PATH.exists():
        keymap_source = str(action_map.DEFAULT_KEYMAP_PATH)
    else:
        keymap_source = "built-in defaults"

    print("Fire TV receiver PoC (DIAL + Turnstile)")
    print(f"  device:      {name}  ({args.model})")
    print(f"  UDN:         {udn}")
    print(f"  DIAL:        {dial_cfg.application_url}{dial.DIAL_APP}")
    print(f"  HTTPS:       https://{bind_ip}:{args.https_port}  (bound to {https_bind})")
    print(f"  TLS cert:    {cert_path}")
    print(f"  cert SHA256: {certificate_fingerprint(cert_path)}")
    if args.inject == "ydotool":
        print("  actions:     live (ydotool + commands)")
    else:
        print("  actions:     log only (dry run; add --inject ydotool to execute)")
    print(f"  keymap:      {keymap_source}")
    print(f"  PIN display: {'Qt card' if pin_display.available() else 'terminal/journal'}")
    print(f"  apps:        {', '.join(str(app['name']) for app in apps_list) or '(none)'}")
    print(f"  app icons:   {len(icon_files)} local")
    if voice_server is not None:
        wss = f"wss://{bind_ip}:{voice_cfg.get('port', 9090)}/"
        print(f"  voice:       {wss}  (PCM s16le 16 kHz mono)")
        print(f"  voice STT:   {voice.stt_status(voice_cfg)}")
        print(f"  voice route: {voice_cfg.get('mode', 'command')}")
    elif voice_external:
        wss = f"wss://{bind_ip}:{voice_cfg.get('port', 9090)}/"
        print(f"  voice:       external {wss} (voice-stt.service)")
        print("  voice STT:   shared faster-whisper model")
    else:
        print("  voice:       disabled")
    print()
    print("On the phone:")
    print("  1. Official Amazon Fire TV app -> Connect to Fire TV -> " + name)
    print("  2. Select it. The app should move from DIAL to HTTPS and ask for a PIN.")
    print("  3. The 4-digit PIN appears in THIS terminal. Enter it in the app.")
    print()
    print("If the app shows a connection error and this terminal prints")
    print("[tls] handshake/connection error ..., the app rejected the")
    print("self-signed certificate. That is the result we need to know.")
    print("Press Ctrl+C to stop.")
    sys.stdout.flush()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        pin_display.close()
        if voice_server is not None:
            voice_server.close()
        runner.close()
        ssdp.close()
        dial.stop_servers(desc_server, apps_server)
        https_server.shutdown()
        https_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
