#!/usr/bin/env python3
"""Minimal Fire TV DIAL/SSDP discovery PoC.

Goal: make a Linux box appear in the official Amazon Fire TV app's
"Connect to Fire TV" list. This tests discovery only. It deliberately does
not implement the HTTPS/Turnstile pairing step, so selecting the device in
the app will fail after it is listed.

Wire flow implemented (all stdlib):

    phone -> SSDP M-SEARCH (UDP 239.255.255.250:1900)
    box   -> HTTP/1.1 200 with LOCATION: http://<ip>:60000/dd.xml
    phone -> GET /dd.xml
    box   -> XML + Application-URL: http://<ip>:8009/apps/
    phone -> GET /apps/FireTVRemote
    box   -> 200 <service><name>FireTVRemote</name><state>running</state></service>

Run on the Linux box:

    python3 -m receiver.dial --name "Living Room Linux"

Then open the official Amazon Fire TV app on the phone, same Wi-Fi, and look
for the device in "Connect to Fire TV".

Self-test (no phone needed):

    python3 -m receiver.dial --selftest
"""

from __future__ import annotations

import argparse
import http.server
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"
DIAL_APP = "FireTVRemote"
DESC_PORT = 60000
APPS_PORT = 8009


def detect_lan_ip() -> str:
    """Return the IPv4 address the host would use for a default route."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # no packet is sent for UDP connect
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def default_device_name() -> str:
    """Use the OS hostname, with a neutral fallback for nameless hosts."""
    hostname = socket.gethostname().strip()
    # Drop any DNS suffix (myhost.local -> myhost) so the app's list
    # stays readable. Keep the user's explicit --name untouched.
    short = hostname.split(".", 1)[0].strip()
    return short or "Linux TV"


def build_description(name: str, model: str, udn: str) -> bytes:
    return (
        '<?xml version="1.0"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0" '
        'xmlns:r="urn:restful-tv-org:schemas:upnp-dd">\n'
        "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
        "  <device>\n"
        "    <deviceType>urn:schemas-upnp-org:device:tvdevice:1</deviceType>\n"
        f"    <friendlyName>{name}</friendlyName>\n"
        "    <manufacturer>Amazon</manufacturer>\n"
        f"    <modelName>{model}</modelName>\n"
        f"    <UDN>{udn}</UDN>\n"
        "  </device>\n"
        "</root>\n"
    ).encode()


def build_ssdp_response(udn: str, location: str) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        f"ST: {DIAL_ST}\r\n"
        f"USN: {udn}::{DIAL_ST}\r\n"
        f"LOCATION: {location}\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        "EXT:\r\n"
        "SERVER: Linux/4.4.120 UPnP/1.0 Cling/2.0\r\n"
        "\r\n"
    ).encode()


def build_app_xml() -> bytes:
    return (
        b"<service><name>FireTVRemote</name><state>running</state></service>"
    )


@dataclass
class Config:
    name: str
    model: str
    udn: str
    bind_ip: str
    ssdp_port: int
    desc_port: int
    apps_port: int
    verbose: bool

    @property
    def location(self) -> str:
        return f"http://{self.bind_ip}:{self.desc_port}/dd.xml"

    @property
    def application_url(self) -> str:
        return f"http://{self.bind_ip}:{self.apps_port}/apps/"


class QuietHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DescriptionHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FireTVDialPoC/0.1"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        if getattr(self.server, "verbose", False):
            print(f"[dd.xml] {self.address_string()} {fmt % args}", flush=True)

    def _send_description(self, include_body: bool) -> None:
        cfg: Config = self.server.cfg  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        if path != "/dd.xml":
            self.send_error(404)
            return
        body = build_description(cfg.name, cfg.model, cfg.udn)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Application-URL", cfg.application_url)
        self.send_header("Server", "Linux/4.4.120 UPnP/1.0 Cling/2.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._send_description(True)

    def do_HEAD(self) -> None:
        self._send_description(False)


class DialHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FireTVDialPoC/0.1"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        if getattr(self.server, "verbose", False):
            print(f"[dial] {self.address_string()} {fmt % args}", flush=True)

    def _is_app(self) -> bool:
        return self.path.split("?", 1)[0].rstrip("/") == f"/apps/{DIAL_APP}"

    def _icon(self) -> tuple[bytes, str] | None:
        path = self.path.split("?", 1)[0]
        if not path.startswith("/apps/icon/"):
            return None
        name = path.rsplit("/", 1)[-1]
        name = re.sub(r"\._S[XY]\d+_(?=\.[A-Za-z0-9]+$)", "", name)
        icons: dict[str, bytes] = getattr(self.server, "icons", {})
        data = icons.get(name)
        if data is None:
            return None
        if data.startswith(b"\x89PNG"):
            ctype = "image/png"
        elif data.startswith(b"\xff\xd8"):
            ctype = "image/jpeg"
        elif data.startswith(b"GIF8"):
            ctype = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            ctype = "image/webp"
        else:
            ctype = "application/octet-stream"
        return data, ctype

    def _send_icon(self, include_body: bool) -> bool:
        icon = self._icon()
        if icon is None:
            return False
        data, ctype = icon
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if include_body:
            self.wfile.write(data)
        return True

    def do_GET(self) -> None:
        if self._send_icon(True):
            return
        if not self._is_app():
            self.send_error(404)
            return
        body = build_app_xml()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if self._send_icon(False):
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._is_app():
            self.send_error(404)
            return
        # The official app sends an empty text/plain POST before pairing and
        # an occasional x-api-key recovery POST. It only cares about 2xx.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(201)
        self.send_header("Location", f"/apps/{DIAL_APP}/run")
        self.send_header("Content-Length", "0")
        self.end_headers()


class SSDPResponder(threading.Thread):
    def __init__(self, cfg: Config) -> None:
        super().__init__(daemon=True, name="ssdp")
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", cfg.ssdp_port))
        self.sock.settimeout(0.5)

        if cfg.bind_ip not in ("127.0.0.1", "0.0.0.0"):
            try:
                mreq = struct.pack(
                    "4s4s",
                    socket.inet_aton(SSDP_ADDR),
                    socket.inet_aton(cfg.bind_ip),
                )
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self.sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(cfg.bind_ip),
                )
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            except OSError as exc:
                print(
                    f"warning: could not join multicast group on {cfg.bind_ip}: {exc}\n"
                    "         unicast M-SEARCH to this host may still work.",
                    file=sys.stderr,
                    flush=True,
                )

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            if "M-SEARCH" not in text.upper():
                continue
            match = re.search(r"(?im)^ST:\s*(\S+)", text)
            if not match or DIAL_ST.lower() not in match.group(1).lower():
                continue
            response = build_ssdp_response(self.cfg.udn, self.cfg.location)
            try:
                self.sock.sendto(response, addr)
            except OSError as exc:
                print(f"warning: SSDP reply to {addr} failed: {exc}", file=sys.stderr)
                continue
            if self.cfg.verbose:
                print(f"[ssdp] M-SEARCH from {addr[0]}:{addr[1]} -> 200", flush=True)

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass


def start_servers(cfg: Config) -> tuple[QuietHTTPServer, QuietHTTPServer]:
    desc = QuietHTTPServer(("", cfg.desc_port), DescriptionHandler)
    desc.cfg = cfg  # type: ignore[attr-defined]
    desc.verbose = cfg.verbose  # type: ignore[attr-defined]
    apps = QuietHTTPServer(("", cfg.apps_port), DialHandler)
    apps.cfg = cfg  # type: ignore[attr-defined]
    apps.verbose = cfg.verbose  # type: ignore[attr-defined]
    apps.icons = {}  # type: ignore[attr-defined]
    threading.Thread(target=desc.serve_forever, daemon=True, name="dd.xml").start()
    threading.Thread(target=apps.serve_forever, daemon=True, name="dial").start()
    return desc, apps


def stop_servers(desc: QuietHTTPServer, apps: QuietHTTPServer) -> None:
    for server in (desc, apps):
        server.shutdown()
        server.server_close()


def selftest() -> int:
    cfg = Config(
        name="PoC Linux HTPC",
        model="AFTMA08C15",
        udn=f"uuid:{uuid.uuid4()}",
        bind_ip="127.0.0.1",
        ssdp_port=19000,
        desc_port=16000,
        apps_port=18009,
        verbose=True,
    )
    desc, apps = start_servers(cfg)
    ssdp = SSDPResponder(cfg)
    ssdp.start()
    time.sleep(0.2)
    try:
        query = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{cfg.ssdp_port}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 1\r\n"
            f"ST: {DIAL_ST}\r\n\r\n"
        ).encode()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2.0)
        client.sendto(query, ("127.0.0.1", cfg.ssdp_port))
        response, _ = client.recvfrom(4096)
        text = response.decode("utf-8", errors="replace")
        assert text.startswith("HTTP/1.1 200"), text
        location = re.search(r"(?im)^LOCATION:\s*(\S+)", text)
        assert location, text
        with urllib.request.urlopen(location.group(1), timeout=2.0) as resp:
            description = resp.read().decode("utf-8")
            app_url = resp.headers["Application-URL"]
        assert "<friendlyName>PoC Linux HTPC</friendlyName>" in description
        assert app_url == cfg.application_url, (app_url, cfg.application_url)
        with urllib.request.urlopen(app_url + DIAL_APP, timeout=2.0) as resp:
            assert resp.status == 200
        print("PASS: SSDP -> LOCATION -> dd.xml -> Application-URL -> GET FireTVRemote")
        print(f"      device: {cfg.name} | dial: {app_url}{DIAL_APP}")
        return 0
    finally:
        ssdp.close()
        stop_servers(desc, apps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Minimal Fire TV DIAL/SSDP discovery PoC",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--name",
        default=None,
        help="name shown in the app; defaults to the OS hostname, then 'Linux TV'",
    )
    parser.add_argument("--model", default="AFTMA08C15", help="Fire TV model string")
    parser.add_argument("--udn", default=None, help="stable UUID; random if omitted")
    parser.add_argument(
        "--bind",
        default=None,
        help="LAN IPv4 address to advertise; auto-detected if omitted",
    )
    parser.add_argument("--ssdp-port", type=int, default=SSDP_PORT)
    parser.add_argument("--desc-port", type=int, default=DESC_PORT)
    parser.add_argument("--apps-port", type=int, default=APPS_PORT)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run a loopback wire test and exit (no phone needed)",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    bind_ip = args.bind or detect_lan_ip()
    cfg = Config(
        name=args.name or default_device_name(),
        model=args.model,
        udn=args.udn or f"uuid:{uuid.uuid4()}",
        bind_ip=bind_ip,
        ssdp_port=args.ssdp_port,
        desc_port=args.desc_port,
        apps_port=args.apps_port,
        verbose=args.verbose,
    )

    desc, apps = start_servers(cfg)
    ssdp = SSDPResponder(cfg)
    ssdp.start()

    print("Fire TV DIAL/SSDP PoC")
    print(f"  device:      {cfg.name}  ({cfg.model})")
    print(f"  UDN:         {cfg.udn}")
    print(f"  SSDP:        {SSDP_ADDR}:{cfg.ssdp_port}")
    print(f"  description: {cfg.location}")
    print(f"  DIAL:        {cfg.application_url}{DIAL_APP}")
    print()
    print("Open the official Amazon Fire TV app on the phone (same Wi-Fi),")
    print('go to "Connect to Fire TV", and look for the device name above.')
    print()
    print("This PoC tests discovery only. Selecting the device will fail at")
    print("the HTTPS/Turnstile pairing step; that is expected for now.")
    print()
    print("If it is not listed: check AP/client isolation, multicast, the")
    print("--bind interface, and that UDP 1900 + TCP 60000/8009 are allowed.")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        ssdp.close()
        stop_servers(desc, apps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
