#!/usr/bin/env python3
"""Voice-button speech-to-text daemon for the Fire TV remote.

Sits on the remote's hidraw node, waits for the voice button, records the
Opus stream while held, and on release decodes + transcribes it, then routes
the result through the command handler:

  * Commands ("open firefox", "search for my notes") → launch app / search file
  * Dictation (everything else) → type into the focused window

This is push-to-talk only — no always-listening system mic. The remote's voice
button is the trigger, consistent with the fire-remote-client project.

Usage:
    python3 scripts/06_voice_stt.py --mac AA:BB:CC:DD:EE:FF
    python3 scripts/06_voice_stt.py --mac .. --model small --type

Typing uses `ydotool` (via `ydotoold`) on Wayland; `wtype`/`xdotool` are
fallbacks only. Clipboard uses wl-copy (Wayland) / xclip (X11) when available.

Needs read/write access to the remote's /dev/hidrawN. Either run as root, or
install the udev rule from udev/91-fire-remote.rules (grants the `input`
group, which most desktop users are in) and run as your user.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
for _path in (str(REPO_ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from voice_core import Transcriber, load_config  # noqa: E402

from receiver.hidraw import RemoteNotFound, find_remote_hidraw  # noqa: E402

SYS_HIDRAW = "/sys/class/hidraw"
VOICE_KEY = 0x21  # legacy byte check; VOICE_USAGE is the 16-bit consumer usage
VOICE_USAGE = 0x0221


def find_hidraw(mac: str | None = None) -> str:
    try:
        return find_remote_hidraw(mac)
    except RemoteNotFound as exc:
        raise SystemExit(str(exc)) from None


def parse_report_descriptor(buf: bytes) -> dict[int, dict]:
    reports: dict[int, dict] = {}
    rid = rsize = rcount = 0
    i = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        if b == 0xFE:
            if i < len(buf):
                i += 2 + buf[i]
            continue
        tag, typ, size = b >> 4, (b >> 2) & 3, b & 3
        nbytes = (0, 1, 2, 4)[size]
        data = int.from_bytes(buf[i:i + nbytes], "little") if nbytes else 0
        i += nbytes
        if typ == 1:
            if tag == 0x7:
                rsize = data
            elif tag == 0x9:
                rcount = data
            elif tag == 0x8:
                rid = data
        elif typ == 0 and tag in (0x8, 0x9):
            direction = "input" if tag == 0x8 else "output"
            r = reports.setdefault(rid, {"input": 0, "output": 0})
            r[direction] += rcount * rsize // 8
    return reports


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def iter_reports(data: bytes, sizes: dict[int, int]):
    """Split one hidraw read into (report-id, payload) records.

    hidraw normally returns one report per read, but HID stacks and some
    kernels can coalesce reports. Sizes come from the report descriptor
    (payload bytes, excluding the report-id byte). An unknown id is treated as
    a desync: drop the rest of this read rather than misparse it as audio.
    """
    off = 0
    while off < len(data):
        rid = data[off]
        plen = sizes.get(rid)
        if plen is None or plen <= 0:
            log(f".. unknown/empty HID report id 0x{rid:02x}; dropping rest of read")
            return
        total = 1 + plen
        if off + total > len(data):
            log(".. short HID report at end of read; dropping")
            return
        yield rid, data[off + 1:off + total]
        off += total


def consumer_usages(payload: bytes) -> list[int]:
    """Return the 16-bit consumer usages in a report 0x02 payload."""
    return [int.from_bytes(payload[i:i + 2], "little")
            for i in range(0, len(payload) - 1, 2)]


class UtteranceWorker(threading.Thread):
    """Transcribe/route/type in the background so capture never blocks.

    The old code decoded + ran Whisper + typed inside the hidraw read loop.
    During a long transcription or a 10 s ydotool timeout the next voice press
    was queued, the reactive mic-enable was written late, and hidraw dropped
    the beginning/end of the utterance.
    """

    def __init__(self, stt: Transcriber, handler, args, maxsize: int = 8):
        super().__init__(name="stt-worker", daemon=True)
        self.stt = stt
        self.handler = handler
        self.args = args
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)

    def submit(self, packets: list[bytes], secs: float) -> None:
        try:
            self.q.put_nowait(("opus", packets, secs))
        except queue.Full:
            log("!! transcription queue full; dropping utterance")

    def submit_pcm(self, pcm: bytes, secs: float) -> None:
        """Phone-app WSS audio is already decoded PCM s16le."""
        try:
            self.q.put_nowait(("pcm", pcm, secs))
        except queue.Full:
            log("!! transcription queue full; dropping phone utterance")

    def stop(self) -> None:
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def run(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                kind, payload, secs = item
                self._process(kind, payload, secs)
            except Exception:  # noqa: BLE001
                log("!! utterance worker failed:\n" + traceback.format_exc())
            finally:
                self.q.task_done()

    def _process(self, kind: str, payload: Any, secs: float) -> None:
        if kind == "pcm":
            pcm = bytes(payload)
        else:
            import opuslib

            dec = opuslib.Decoder(16000, 1)
            pcm = bytearray()
            dropped = 0
            for p in payload:
                try:
                    pcm += dec.decode(p, 320)
                except Exception:  # noqa: BLE001
                    dropped += 1
            if dropped:
                log(f".. dropped {dropped}/{len(payload)} undecodable Opus frames")
            pcm = bytes(pcm)
        text = self.stt.transcribe_pcm16(pcm)
        if text:
            log(f">> transcript: {text}")
            if self.args.print:
                print(text, flush=True)
            deliver(text, self.args, self.handler)
        else:
            log(">> no speech detected.")


def deliver(text: str, args, handler) -> None:
    """Route transcript through the shared safe command/dictation gate.

    ``voice_core.route_text`` is the same logic the phone app's WSS voice uses:
    only short, explicit commands with known app targets execute; everything
    else is dictation. This prevents "open Firefox" embedded in a dictated
    sentence from launching an app.
    """
    import voice_core

    safe = bool(getattr(args, "safe_commands", True))
    result = voice_core.route_text(
        text, handler, mode="command", safe=safe, log=log
    )
    log(result)

    # Optional extras that the handler doesn't do itself.
    if args.copy:
        clip = shutil.which("wl-copy") or shutil.which("xclip")
        if clip is None:
            log("!! --copy: no wl-copy/xclip found")
        elif "wl-copy" in clip:
            subprocess.run([clip], input=text.encode(), check=False)
        else:
            subprocess.run([clip, "-selection", "clipboard"],
                           input=text.encode(), check=False)

    if args.notify and shutil.which("notify-send"):
        subprocess.run(["notify-send", "Voice", text], check=False)


def wait_hidraw(mac: str) -> str:
    while True:
        try:
            return find_hidraw(mac)
        except SystemExit as e:
            log(f">> {e} retrying in 5s… (wake the remote)")
            time.sleep(5)


def check_no_other_daemon(mac: str) -> None:
    """Refuse to start if another voice daemon is already bound to this remote.

    Two daemons reading the same hidraw node each transcribe and type every
    utterance, producing interleaved garbage. Only one may run at a time.
    """
    me = os.getpid()
    try:
        out = subprocess.run(
            ["pgrep", "-af", "voice_stt|voice_assistant"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return  # pgrep unavailable — skip the check rather than block startup

    others = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmd = line.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == me:
            continue
        # Only count real daemons: a python process running one of our scripts
        # with this MAC. Skip shells and `uv run` wrappers to avoid false positives.
        if "python" not in cmd.split(" ")[0]:
            continue
        if not re.search(r"(06_voice_stt|voice_assistant)\.py", cmd):
            continue
        if mac.lower() not in cmd.lower():
            continue
        others.append((pid, cmd[:90]))

    if others:
        log("!! ANOTHER voice daemon is already running for this remote:")
        for pid, cmd in others:
            log(f"     pid {pid}: {cmd}")
        log("!! Every daemon types each utterance — output will be doubled/garbled.")
        log("!! Stop the others first, e.g.: systemctl --user stop "
            "voice-stt.service voice-assistant.service")
        raise SystemExit(1)


def start_wss(args, worker_ref: dict[str, Any]):
    """Serve the phone app's WSS 9090 stream using this daemon's model.

    The socket is bound before the Whisper model loads so the receiver sees
    9090 as busy and delegates voice to this process instead of starting a
    second model. ``worker_ref`` is filled in once the model is ready.
    """
    import json

    sys.path.insert(0, str(REPO_ROOT))
    from receiver import dial
    from receiver.devices import DeviceStore
    from receiver.voice import WssPcmServer

    path = (
        Path(args.receiver_config).expanduser()
        if args.receiver_config
        else Path.home() / ".config" / "firetv-receiver" / "config.json"
    )
    rcfg: dict = {}
    try:
        if path.exists():
            rcfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"!! cannot read receiver config {path}: {exc}")

    bind = args.wss_bind or rcfg.get("bind") or dial.wait_for_lan_ip(10.0)
    if not bind:
        log("!! WSS disabled: no usable LAN IPv4 address (connect to Wi-Fi/Ethernet)")
        return None
    port = args.wss_port or int(rcfg.get("voice_port", 9090))
    cert_dir = Path(
        str(rcfg.get("cert_dir") or "~/.config/firetv-receiver/poc-tls")
    ).expanduser()
    cert = cert_dir / "cert.pem"
    key = cert_dir / "key.pem"
    if not cert.exists() or not key.exists():
        log(
            f"!! WSS disabled: certificate missing in {cert_dir}; "
            "start firetv-receiver once to generate it"
        )
        return None

    store = DeviceStore()

    def on_pcm(pcm: bytes, addr) -> None:
        secs = len(pcm) / 32000.0
        if secs < args.min_secs:
            log(f".. phone utterance too short ({secs:.2f}s); ignored")
            return
        log(f">> phone voice: {secs:.2f}s from {addr[0]}")
        worker = worker_ref.get("worker")
        if worker is None:
            log(".. phone voice arrived before the model was ready; ignored")
            return
        worker.submit_pcm(pcm, secs)

    server = WssPcmServer(
        bind_ip=bind,
        cert_path=cert,
        key_path=key,
        port=port,
        api_key="0987654321",
        token_ok=store.valid,
        on_pcm=on_pcm,
        log=log,
    )
    try:
        server.start()
    except OSError as exc:
        log(f"!! WSS disabled on {bind}:{port}: {exc}")
        return None
    log(f">> WSS phone voice on wss://{bind}:{port}/ (shared model)")
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mac", default="",
                    help="remote BLE MAC (default: auto-detect the Amazon remote)")
    ap.add_argument("--model", default=None,
                    help="whisper model: tiny/base/small/medium/large-v3 "
                         "(default: stt.model from config, else small)")
    ap.add_argument("--language", default=None,
                    help="Whisper language (default: stt.language from config, else en)")
    ap.add_argument("--min-secs", type=float, default=None,
                    help="ignore utterances shorter than this "
                         "(default: command.min_secs from config, else 0.4)")
    ap.add_argument("--print", dest="do_print", action="store_true", default=True,
                    help="print text to stdout (default on)")
    ap.add_argument("--no-print", dest="do_print", action="store_false")
    ap.add_argument("--type", dest="do_type", action="store_true",
                    help="type dictation text into the focused window")
    ap.add_argument("--copy", action="store_true", help="copy text to clipboard")
    ap.add_argument("--notify", action="store_true", help="desktop notification")
    ap.add_argument("--wss", action="store_true",
                    help="also serve the phone app's WSS 9090 voice stream "
                         "through this daemon's model/handler")
    ap.add_argument("--wss-bind", default=None,
                    help="WSS bind address (default: receiver config or LAN IP)")
    ap.add_argument("--wss-port", type=int, default=None,
                    help="WSS port (default: receiver config, else 9090)")
    ap.add_argument("--receiver-config", default=None,
                    help="receiver config.json (default: ~/.config/firetv-receiver/config.json)")
    args = ap.parse_args()
    args.print = args.do_print
    args.type = args.do_type

    check_no_other_daemon(args.mac)

    cfg = load_config()
    stt_cfg = cfg.get("stt", {})
    args.safe_commands = bool(cfg.get("command", {}).get("safe_commands", True))
    if args.min_secs is None:
        args.min_secs = float(cfg.get("command", {}).get("min_secs", 0.4))

    # Bind WSS first so firetv-receiver always delegates 9090 to this daemon.
    # Otherwise the receiver can win the port while Whisper is still loading
    # and start its own second model.
    worker_ref: dict[str, Any] = {}
    wss = start_wss(args, worker_ref) if args.wss else None

    stt = Transcriber(
        model=args.model or stt_cfg.get("model", "small"),
        language=args.language or stt_cfg.get("language", "en"),
        device=stt_cfg.get("device", "cpu"),
        compute_type=stt_cfg.get("compute_type", "int8"),
        hotwords=stt_cfg.get("hotwords", ""),
        initial_prompt=stt_cfg.get("initial_prompt"),
        beam_size=int(stt_cfg.get("beam_size", 5)),
    )

    # Import command handler for action routing (no STT vocabulary injection)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import importlib
    _cmd = importlib.import_module("08_command_handler")
    handler = _cmd.CommandHandler(cfg)

    worker = UtteranceWorker(stt, handler, args)
    worker_ref["worker"] = worker
    worker.start()

    log(">> ready. Hold the voice button, speak, release.")
    log(">> capture is decoupled from transcription/typing")

    fd = None
    try:
        while True:  # outer reconnect loop: survives link drops (new hidraw node)
            dev = wait_hidraw(args.mac)
            rdesc = Path(SYS_HIDRAW, os.path.basename(dev),
                         "device", "report_descriptor").read_bytes()
            reports = parse_report_descriptor(rdesc)
            inputs = {r: v["input"] for r, v in reports.items() if v.get("input")}
            outputs = {r: v["output"] for r, v in reports.items() if v.get("output")}
            sizes = {r: v["input"] for r, v in reports.items() if v.get("input")}
            audio_id = max(inputs, key=inputs.get)
            enable_id = min(outputs, key=outputs.get)
            log(f">> {dev} audio=0x{audio_id:02x} enable=0x{enable_id:02x}")

            try:
                fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
            except OSError as e:
                log(f"!! cannot open {dev}: {e} (udev rule or root needed)")
                time.sleep(5)
                continue

            # Reset the mic: a previous daemon killed mid-utterance can leave the
            # remote streaming audio with no press event to trigger recording,
            # which silently discards everything. Force a disable, drain, then
            # re-enable.
            try:
                os.write(fd, bytes([enable_id, 0x00]))
                time.sleep(0.15)
                os.write(fd, bytes([enable_id, 0x01]))
                time.sleep(0.15)
                os.write(fd, bytes([enable_id, 0x00]))
                time.sleep(0.15)
                while select.select([fd], [], [], 0)[0]:
                    os.read(fd, 4096)
                log(">> mic reset (cleared stale stream state)")
            except OSError as e:
                log(f".. mic reset skipped: {e}")

            packets: list[bytes] = []
            enabled = False
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 1.0)
                    if not r:
                        continue
                    lost = False
                    while True:
                        try:
                            data = os.read(fd, 4096)
                        except BlockingIOError:
                            break
                        except OSError as e:
                            log(f"!! hidraw lost ({e}) — waiting for reconnect…")
                            lost = True
                            break
                        if not data:
                            break
                        for rid, payload in iter_reports(data, sizes):
                            if rid == audio_id:
                                # Only capture audio while voice is held.
                                if enabled:
                                    packets.append(bytes(payload))
                                continue
                            if rid != 0x02:
                                continue  # keyboard/vendor reports don't end voice
                            usages = consumer_usages(payload)
                            if enabled and VOICE_USAGE not in usages:
                                # Voice released (even if another key is still down).
                                enabled = False
                                try:
                                    os.write(fd, bytes([enable_id, 0x00]))
                                except OSError:
                                    pass
                                n = len(packets)
                                secs = n * 0.02
                                log(f">> voice release: {n} frames ({secs:.1f}s)")
                                if secs >= args.min_secs:
                                    worker.submit(list(packets), secs)
                                else:
                                    log(".. utterance too short; ignored")
                                packets = []
                                continue
                            if not enabled and VOICE_USAGE in usages:
                                enabled = True
                                packets = []
                                try:
                                    os.write(fd, bytes([enable_id, 0x01]))
                                except OSError as e:
                                    log(f"!! enable failed: {e}")
                                log(">> voice press: recording…")
                                continue
                    if lost:
                        break  # back to outer loop: re-resolve hidraw node
            finally:
                # Always turn the mic off on exit so a killed daemon can't leave
                # the remote streaming with no press event to trigger recording.
                if enabled:
                    try:
                        os.write(fd, bytes([enable_id, 0x00]))
                    except OSError:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
    except KeyboardInterrupt:
        log(">> stopping (Ctrl-C)")
    finally:
        if wss is not None:
            wss.close()
        worker.stop()
        worker.join(timeout=10)
        if worker.is_alive():
            log("!! worker still busy at shutdown; daemon exit will end it")


if __name__ == "__main__":
    main()