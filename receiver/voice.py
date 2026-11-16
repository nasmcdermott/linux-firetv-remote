#!/usr/bin/env python3
"""Voice capture for the official Fire TV app: WSS 9090 + raw PCM.

The app opens a WebSocket **after** ``POST /v1/FireTV/voiceCommand?action=start``
and streams raw PCM:

* ``wss://<box>:9090/``
* same ``x-api-key`` / ``x-client-token`` headers as the HTTP request
* binary frames, s16le, 16 kHz, mono, no container

This module implements the WebSocket handshake and framing with the standard
library. Transcripts are routed through the **same** faster-whisper model and
command handler as the BLE remote (``scripts/voice_core.py``), so the phone app
and the physical remote share one STT/command pipeline.

The receiver can run with the project venv (which has faster-whisper) or any
Python that can import it. If it cannot, the WAV is still saved and the path is
printed. The Fire TV app pins a certificate on Android but accepted the
self-signed certificate on iOS; voice uses the same TLS identity as HTTPS 8080.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "voice.json"

DEFAULT_VOICE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "port": 9090,
    "stt": True,
    # command: run the FRC CommandHandler (launch/search/type fallback)
    # dictation: always type the transcript
    # log: print only (useful for testing)
    "mode": "command",
    "safe_commands": True,
    "save_audio": True,
    "save_dir": str(Path.home() / ".cache" / "firetv-receiver" / "voice"),
    "min_seconds": 0.3,
}

SAMPLE_RATE = 16000
SAMPLE_BYTES = 2
CHANNELS = 1
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HANDSHAKE = 65536


def load_voice_config(path: Path | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_VOICE_CONFIG)
    source = path if path is not None else DEFAULT_CONFIG_PATH
    if source is None or not source.exists():
        return cfg
    try:
        override = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read voice config {source}: {exc}") from None
    if not isinstance(override, dict):
        raise SystemExit(f"voice config {source} must contain a JSON object")
    cfg.update(override)
    return cfg


def stt_status(cfg: dict[str, Any]) -> str:
    """Describe how transcripts will be produced, for the startup banner."""
    if not cfg.get("stt", True):
        return "off (WAV only)"
    import importlib.util

    if importlib.util.find_spec("faster_whisper") is None:
        return "unavailable (WAV only; run with the project venv)"
    try:
        import voice_core  # noqa: PLC0415 - lazy: only with the voice venv

        return voice_core.describe()
    except Exception as exc:  # noqa: BLE001 - optional module
        return f"unavailable ({exc})"


def _log(message: str) -> None:
    print(message, flush=True)


class _Reader:
    """Read exactly n bytes, including bytes already buffered after headers."""

    def __init__(self, conn: socket.socket, initial: bytes = b"") -> None:
        self.conn = conn
        self.buf = initial

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.conn.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _read_frame(reader: _Reader) -> tuple[bool, int, bytes]:
    b1, b2 = reader.read(2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", reader.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", reader.read(8))[0]
    if length > 4 * 1024 * 1024:
        raise EOFError("websocket frame too large")
    mask = reader.read(4) if masked else b""
    payload = reader.read(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return fin, opcode, payload


def _send_frame(conn: socket.socket, opcode: int, payload: bytes = b"") -> None:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    conn.sendall(header + payload)


"""Transcription and command routing are provided by scripts/voice_core.py.

The receiver does not carry its own Whisper wrapper: the BLE remote's model,
hotwords, initial prompt and CommandHandler are the single source of truth.
The pipeline is imported lazily on the first utterance so startup stays fast
and the system Python can still run the button/DIAL server without numpy.
"""


class VoiceServer:
    """Minimal TLS WebSocket server for the app's raw-PCM voice stream."""

    def __init__(
        self,
        bind_ip: str,
        cert_path: Path,
        key_path: Path,
        cfg: dict[str, Any],
        api_key: str,
        token_ok: Callable[[str | None], bool],
    ) -> None:
        self.bind_ip = bind_ip
        self.cert_path = cert_path
        self.key_path = key_path
        self.cfg = cfg
        self.api_key = api_key
        self.token_ok = token_ok
        self._pipeline_lock = threading.Lock()
        self._transcriber: Any = None
        self._handler: Any = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._session_lock = threading.Lock()
        self._active = 0

    def _ensure_pipeline(self) -> tuple[Any, Any]:
        """Load the shared FRC Whisper model + CommandHandler on first use."""
        with self._pipeline_lock:
            if self._handler is None:
                import voice_core  # noqa: PLC0415 - lazy, needs the voice venv

                self._transcriber = voice_core.build_transcriber()
                self._handler, _cmd = voice_core.build_handler()
            return self._transcriber, self._handler

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(self.cert_path), keyfile=str(self.key_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_ip, int(self.cfg.get("port", 9090))))
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = context.wrap_socket(sock, server_side=True)
        self._thread = threading.Thread(target=self._accept_loop, name="voice-wss", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except ssl.SSLError as exc:
                _log(f"[voice] TLS handshake failed: {exc}")
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn, addr), name="voice-conn", daemon=True
            ).start()

    # -- session ---------------------------------------------------------

    def _handle(self, conn: ssl.SSLSocket, addr: Any) -> None:
        with self._session_lock:
            self._active += 1
        try:
            conn.settimeout(15.0)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > MAX_HANDSHAKE:
                    return
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if not lines or "websocket" not in head.decode("latin-1").lower():
                self._http_error(conn, 400, "not a websocket request")
                return
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            if headers.get("x-api-key") != self.api_key:
                self._http_error(conn, 403, "bad api key")
                return
            if not self.token_ok(headers.get("x-client-token")):
                self._http_error(conn, 403, "missing client token")
                return

            key = headers.get("sec-websocket-key")
            if not key:
                self._http_error(conn, 400, "missing Sec-WebSocket-Key")
                return
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("ascii"))
            conn.settimeout(60.0)
            _log(f"[voice] stream from {addr[0]} (token ok)")
            pcm = self._receive(conn, rest)
            self._finalize(pcm, addr)
        except (EOFError, OSError, ssl.SSLError) as exc:
            if not isinstance(exc, EOFError):
                _log(f"[voice] connection from {addr[0]} ended: {exc}")
        finally:
            with self._session_lock:
                self._active -= 1
            try:
                conn.close()
            except OSError:
                pass

    def _http_error(self, conn: ssl.SSLSocket, code: int, message: str) -> None:
        body = message.encode("utf-8")
        try:
            conn.sendall(
                f"HTTP/1.1 {code} {message}\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
                + body
            )
        except OSError:
            pass

    def _receive(self, conn: ssl.SSLSocket, initial: bytes) -> bytearray:
        reader = _Reader(conn, initial)
        pcm = bytearray()
        fragments = bytearray()
        fragment_opcode: int | None = None
        try:
            while True:
                fin, opcode, payload = _read_frame(reader)
                if opcode == 0x8:  # close
                    code = payload[:2] if len(payload) >= 2 else b""
                    _send_frame(conn, 0x8, code)
                    break
                if opcode == 0x9:  # ping
                    _send_frame(conn, 0xA, payload)
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode == 0x0:  # continuation
                    if fragment_opcode is None:
                        continue
                    fragments.extend(payload)
                    if fin:
                        if fragment_opcode == 0x2:
                            pcm.extend(fragments)
                        fragments.clear()
                        fragment_opcode = None
                    continue
                if opcode in (0x1, 0x2):
                    if fin:
                        if opcode == 0x2:
                            pcm.extend(payload)
                    else:
                        fragments = bytearray(payload)
                        fragment_opcode = opcode
                    continue
                _log(f"[voice] ignoring websocket opcode 0x{opcode:x}")
        except EOFError:
            _log(f"[voice] stream closed without a close frame ({len(pcm)} bytes)")
        except OSError as exc:
            _log(f"[voice] stream read error: {exc} ({len(pcm)} bytes)")
        return pcm

    # -- finalize --------------------------------------------------------

    def _finalize(self, pcm: bytearray, addr: Any) -> None:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_BYTES)
        min_seconds = float(self.cfg.get("min_seconds", 0.3))
        if seconds < min_seconds:
            _log(f"[voice] ignored {seconds:.2f}s utterance from {addr[0]}")
            return

        default_dir = DEFAULT_VOICE_CONFIG["save_dir"]
        save_dir = Path(str(self.cfg.get("save_dir", default_dir))).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)
        wav_path = save_dir / (
            f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}.wav"
        )
        try:
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(SAMPLE_BYTES)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(bytes(pcm))
        except OSError as exc:
            _log(f"[voice] could not write {wav_path}: {exc}")
            return
        _log(f"[voice] {seconds:.2f}s saved to {wav_path}")

        if self.cfg.get("stt", True):
            threading.Thread(
                target=self._process,
                args=(bytes(pcm), wav_path),
                name="voice-stt",
                daemon=True,
            ).start()
        elif not self.cfg.get("save_audio", True):
            wav_path.unlink(missing_ok=True)

    def _process(self, pcm: bytes, wav_path: Path) -> None:
        keep_audio = bool(self.cfg.get("save_audio", True))
        try:
            transcriber, handler = self._ensure_pipeline()
        except Exception as exc:  # noqa: BLE001 - optional voice venv
            _log(f"[voice] cannot load fire-remote-client STT: {exc}")
            if not keep_audio:
                wav_path.unlink(missing_ok=True)
            return

        try:
            text = transcriber.transcribe_pcm16(pcm)
        except Exception as exc:  # noqa: BLE001 - decoder backends raise many types
            _log(f"[voice] transcription failed: {exc}")
            text = ""

        if text:
            _log(f"[voice] transcript: {text}")
            mode = str(self.cfg.get("mode", "command")).lower()
            safe = bool(self.cfg.get("safe_commands", True))
            try:
                import voice_core  # noqa: PLC0415 - lazy

                result = voice_core.route_text(
                    text, handler, mode, safe=safe, log=_log
                )
                if result:
                    _log(f"[voice] route: {result}")
            except Exception as exc:  # noqa: BLE001 - handler backends vary
                _log(f"[voice] route failed: {exc}")
        else:
            _log("[voice] no transcript")

        if not keep_audio:
            wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PCM-only WSS server for the single-process voice daemon


class WssPcmServer(VoiceServer):
    """TLS WebSocket server that hands raw PCM to a callback.

    Used by ``scripts/06_voice_stt.py --wss`` so the BLE hidraw listener and
    the phone app's microphone share one Whisper model/CommandHandler. It does
    no transcription itself.
    """

    def __init__(
        self,
        bind_ip: str,
        cert_path: Path,
        key_path: Path,
        port: int,
        api_key: str,
        token_ok: Callable[[str | None], bool],
        on_pcm: Callable[[bytes, Any], None],
        log: Callable[[str], None] = _log,
    ) -> None:
        self.bind_ip = bind_ip
        self.cert_path = cert_path
        self.key_path = key_path
        self.port = int(port)
        self.api_key = api_key
        self.token_ok = token_ok
        self.on_pcm = on_pcm
        self.log = log
        self.cfg: dict[str, Any] = {}
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._session_lock = threading.Lock()
        self._active = 0

    def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(self.cert_path), keyfile=str(self.key_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_ip, self.port))
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = context.wrap_socket(sock, server_side=True)
        self._thread = threading.Thread(
            target=self._accept_loop, name="voice-wss", daemon=True
        )
        self._thread.start()

    def _handle(self, conn: ssl.SSLSocket, addr: Any) -> None:
        with self._session_lock:
            self._active += 1
        try:
            conn.settimeout(15.0)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > MAX_HANDSHAKE:
                    return
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if not lines or "websocket" not in head.decode("latin-1").lower():
                self._http_error(conn, 400, "not a websocket request")
                return
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            if headers.get("x-api-key") != self.api_key:
                self._http_error(conn, 403, "bad api key")
                return
            if not self.token_ok(headers.get("x-client-token")):
                self._http_error(conn, 403, "missing client token")
                return

            key = headers.get("sec-websocket-key")
            if not key:
                self._http_error(conn, 400, "missing Sec-WebSocket-Key")
                return
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("ascii"))
            conn.settimeout(60.0)
            self.log(f"[voice] phone stream from {addr[0]} (token ok)")
            pcm = self._receive(conn, rest)
            if pcm:
                self.on_pcm(bytes(pcm), addr)
        except (EOFError, OSError, ssl.SSLError) as exc:
            if not isinstance(exc, EOFError):
                self.log(f"[voice] phone connection from {addr[0]} ended: {exc}")
        finally:
            with self._session_lock:
                self._active -= 1
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Self-test (no phone)

def _self_test() -> int:
    import subprocess as sp

    tmp = Path(tempfile.mkdtemp(prefix="firetv-voice-"))
    cert = tmp / "cert.pem"
    key = tmp / "key.pem"
    if not (cert.exists() and key.exists()):
        result = sp.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
                "-days", "1", "-keyout", str(key), "-out", str(cert),
                "-subj", "/CN=voice-selftest",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"openssl failed: {result.stderr}")

    cfg = dict(DEFAULT_VOICE_CONFIG)
    cfg.update(
        {
            "port": 19090,
            "stt": False,
            "save_audio": True,
            "save_dir": str(tmp),
            "type_text": False,
            "min_seconds": 0.1,
        }
    )
    server = VoiceServer(
        bind_ip="127.0.0.1",
        cert_path=cert,
        key_path=key,
        cfg=cfg,
        api_key="0987654321",
        token_ok=lambda token: token == "selftest",
    )
    server.start()
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", 19090), timeout=5)
        conn = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        key_b64 = base64.b64encode(b"0123456789abcdef").decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: 127.0.0.1:19090\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key_b64}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "x-api-key: 0987654321\r\n"
            "x-client-token: selftest\r\n"
            "Content-Type: application/json; charset=utf-8\r\n\r\n"
        )
        conn.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            response += conn.recv(4096)
        assert b"101" in response.split(b"\r\n", 1)[0], response

        # 0.5 s of a 440 Hz tone, sent as two masked binary frames.
        frames = []
        for half in (0, 1):
            pcm = bytearray()
            for i in range(SAMPLE_RATE // 4):
                import math

                value = int(12000 * math.sin(2 * math.pi * 440 * (half * 0.25 + i / SAMPLE_RATE)))
                pcm += struct.pack("<h", value)
            mask = b"\x11\x22\x33\x44"
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(pcm))
            header = bytes([0x82, 0x80 | 126]) + struct.pack("!H", len(pcm))
            frames.append(header + mask + payload)
        conn.sendall(b"".join(frames))
        mask = b"\x55\x66\x77\x88"
        close_payload = b"\x03\xe8"
        masked_close = bytes(b ^ mask[i % 4] for i, b in enumerate(close_payload))
        conn.sendall(bytes([0x88, 0x80 | 2]) + mask + masked_close)
        conn.close()

        deadline = time.time() + 3
        wavs: list[Path] = []
        while time.time() < deadline:
            wavs = list(tmp.glob("voice-*.wav"))
            if wavs:
                break
            time.sleep(0.05)
        if not wavs:
            raise SystemExit("FAIL: no WAV written")
        with wave.open(str(wavs[0]), "rb") as wav:
            assert wav.getframerate() == SAMPLE_RATE, wav.getframerate()
            assert wav.getnchannels() == CHANNELS, wav.getnchannels()
            assert wav.getnframes() > 0, wav.getnframes()
        print(f"PASS: WSS handshake, PCM capture, WAV write ({wavs[0].name})")
        return 0
    finally:
        server.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    print(json.dumps(load_voice_config(args.config), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
