#!/usr/bin/env python3
"""Unified voice assistant daemon for the Fire TV remote.

Remote-driven only — no always-listening system mic, no wake word on the host
mic. The Fire remote's voice button is the trigger:

  hold voice button → speak → release → transcribe → route through command handler

  * "open firefox"       → launch app (or bring to front if already running)
  * "search for my notes"→ search filesystem and open
  * "type hello world"   → type into focused window
  * anything else        → treated as dictation text, typed into focused window

Runs as a systemd user service. Config: config/voice_assistant.yaml
"""

from __future__ import annotations

import argparse
import glob
import os
import select
import signal
import sys
import threading
import time
from pathlib import Path

import opuslib

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import importlib  # noqa: E402

import voice_core  # noqa: E402
from voice_core import Transcriber, build_transcriber, load_config  # noqa: E402

_cmd = importlib.import_module("08_command_handler")  # noqa: E402
CommandHandler = _cmd.CommandHandler  # noqa: E402

SYS_HIDRAW = "/sys/class/hidraw"
VOICE_KEY = 0x21


# ---------------------------------------------------------------------------
# hidraw + report parsing
# ---------------------------------------------------------------------------

def find_hidraw(mac: str) -> str | None:
    mac = mac.lower()
    for path in glob.glob(f"{SYS_HIDRAW}/hidraw*"):
        try:
            uevent = Path(path, "device", "uevent").read_text().lower()
        except OSError:
            continue
        if f"hid_uniq={mac}" in uevent:
            return f"/dev/{os.path.basename(path)}"
    return None


def parse_reports(buf: bytes) -> dict[int, dict]:
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


# ---------------------------------------------------------------------------
# dictation mode (remote PTT)
# ---------------------------------------------------------------------------

class DictationMode:
    """Watch remote hidraw for voice button → transcribe → handle command."""

    def __init__(self, config: dict, tts: Transcriber, mac: str,
                 handler: CommandHandler):
        self.cfg = config
        self.tts = tts
        self.handler = handler
        self.mac = mac.lower()
        self.min_secs = config.get("command", {}).get("min_secs", 0.4)

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            dev = find_hidraw(self.mac)
            if not dev:
                print(f">> waiting for remote ({self.mac})…",
                      file=sys.stderr)
                time.sleep(5)
                continue

            try:
                rdesc = Path(SYS_HIDRAW, os.path.basename(dev),
                             "device", "report_descriptor").read_bytes()
                reports = parse_reports(rdesc)
                inputs = {r: v["input"] for r, v in reports.items()
                          if v.get("input")}
                outputs = {r: v["output"] for r, v in reports.items()
                           if v.get("output")}
                if not inputs or not outputs:
                    raise OSError("no input/output reports in descriptor")
                audio_id = max(inputs, key=inputs.get)
                enable_id = min(outputs, key=outputs.get)
                print(f">> {dev} audio=0x{audio_id:02x} "
                      f"enable=0x{enable_id:02x}", file=sys.stderr)
            except OSError as e:
                print(f"!! cannot read {dev}: {e}", file=sys.stderr)
                time.sleep(5)
                continue

            try:
                fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
            except OSError as e:
                print(f"!! cannot open {dev}: {e}", file=sys.stderr)
                time.sleep(5)
                continue

            packets: list[bytes] = []
            enabled = False
            try:
                while not stop_event.is_set():
                    r, _, _ = select.select([fd], [], [], 1.0)
                    if not r:
                        continue
                    try:
                        data = os.read(fd, 512)
                    except OSError:
                        break  # reconnect
                    if not data:
                        continue
                    rid, payload = data[0], data[1:]
                    if rid == audio_id:
                        if enabled:
                            packets.append(bytes(payload))
                        continue
                    if set(payload) <= {0}:  # release
                        if enabled:
                            enabled = False
                            try:
                                os.write(fd, bytes([enable_id, 0x00]))
                            except OSError:
                                pass
                            secs = len(packets) * 0.02
                            print(f">> voice release: {len(packets)} frames "
                                  f"({secs:.1f}s)", file=sys.stderr)
                            if secs >= self.min_secs:
                                dec = opuslib.Decoder(16000, 1)
                                pcm = bytearray()
                                for p in packets:
                                    try:
                                        pcm += dec.decode(p, 320)
                                    except Exception:  # noqa: BLE001
                                        pass
                                text = self.tts.transcribe_pcm16(bytes(pcm))
                                if text:
                                    print(f">> transcript: {text}",
                                          file=sys.stderr)
                                    safe = bool(
                                        self.cfg.get("command", {}).get(
                                            "safe_commands", True
                                        )
                                    )
                                    result = voice_core.route_text(
                                        text,
                                        self.handler,
                                        mode="command",
                                        safe=safe,
                                        log=lambda m: print(m, file=sys.stderr),
                                    )
                                    print(result, file=sys.stderr)
                                else:
                                    print(">> no speech detected.",
                                          file=sys.stderr)
                            packets = []
                        continue
                    if VOICE_KEY in payload[:2] and not enabled:  # press
                        enabled = True
                        packets = []
                        try:
                            os.write(fd, bytes([enable_id, 0x01]))
                        except OSError as e:
                            print(f"!! enable failed: {e}", file=sys.stderr)
                        print(">> voice press: recording…", file=sys.stderr)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Unified fire-remote voice "
                                             "assistant (remote-only).")
    ap.add_argument("--mac", required=True, help="Fire remote BLE MAC")
    ap.add_argument("--config", help="path to voice_assistant.yaml override")
    args = ap.parse_args()

    cfg = load_config()

    tts = build_transcriber(cfg)

    handler = CommandHandler(cfg)
    stop_event = threading.Event()

    def shutdown(signum, frame):
        print("\n>> shutting down…", file=sys.stderr)
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    dm = DictationMode(cfg, tts, args.mac, handler)
    t = threading.Thread(target=dm.run_forever, args=(stop_event,), daemon=True)
    t.start()

    print(">> voice assistant running (remote-only). Ctrl-C to stop.",
          file=sys.stderr)
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        t.join(timeout=3)
        print(">> stopped.", file=sys.stderr)


if __name__ == "__main__":
    main()