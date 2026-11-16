#!/usr/bin/env python3
"""Always-listening wake-word detector using pyopen-wakeword.

Listens on the system microphone (PipeWire/ALSA via PortAudio) and, when a
wake word is detected, records a short utterance and transcribes it with
faster-whisper, then hands the result to 08_command_handler.py.

This is the "command mode" counterpart to 06_voice_stt.py ("dictation mode"):
  * 07_wake_word.py / voice_assistant.py  → "open firefox", "search for notes"
  * 06_voice_stt.py                        → hold remote voice button → type into field

Run standalone:
    uv run python scripts/07_wake_word.py              # wake word → command → exit after one
    uv run python scripts/07_wake_word.py --forever    # keep listening for wake words
    uv run python scripts/07_wake_word.py --wake-only  # just print detected wake word

Config: config/voice_assistant.yaml
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
    from pyopen_wakeword import (
        Model as WWModel,
    )
    from pyopen_wakeword import (
        OpenWakeWord,
        OpenWakeWordFeatures,
    )
except ImportError as exc:
    raise SystemExit(
        "wake-word support is optional and is not installed.\n"
        "  uv:  uv sync --extra wakeword\n"
        "  pip: pip install -r requirements-wakeword.txt"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import importlib  # noqa: E402

_cmd = importlib.import_module("08_command_handler")  # noqa: E402
CommandHandler = _cmd.CommandHandler  # noqa: E402


from voice_core import Transcriber, build_transcriber, load_config  # noqa: E402

# Map friendly names to pyopen-wakeword Model enum values.
_BUILTIN_MODELS = {
    "alexa": WWModel.ALEXA,
    "hey_alexa": WWModel.ALEXA,
    "hey_jarvis": WWModel.HEY_JARVIS,
    "jarvis": WWModel.HEY_JARVIS,
    "hey_mycroft": WWModel.HEY_MYCROFT,
    "mycroft": WWModel.HEY_MYCROFT,
    "hey_rhasspy": WWModel.HEY_RHASSPY,
    "rhasspy": WWModel.HEY_RHASSPY,
    "okay_nabu": WWModel.OKAY_NABU,
    "computer": WWModel.HEY_MYCROFT,  # fallback: no "computer" built-in
}


class WakeWordEngine:
    """pyopen-wakeword: features extractor + one or more wake-word models."""

    def __init__(self, model_names: list[str], threshold: float,
                 sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.detected = threading.Event()
        self.detected_word: str | None = None
        self._stop = threading.Event()

        print(">> loading pyopen-wakeword features…", file=sys.stderr)
        self.fe = OpenWakeWordFeatures.from_builtin()

        self.models: dict[str, OpenWakeWord] = {}
        for name in model_names:
            ww_model = _BUILTIN_MODELS.get(name.lower())
            if ww_model is None:
                print(f"!! unknown wake word '{name}', skipping",
                      file=sys.stderr)
                continue
            print(f">> loading wake word '{name}'…", file=sys.stderr)
            self.models[name] = OpenWakeWord.from_builtin(ww_model)
        if not self.models:
            raise SystemExit("no valid wake-word models loaded")

        print(f">> wake-word engine ready: {list(self.models)}", file=sys.stderr)

    def _process_chunk(self, pcm16: bytes) -> str | None:
        """Run one audio chunk through features + all models. Returns detected word or None."""
        embeddings = list(self.fe.process_streaming(pcm16))
        if not embeddings:
            return None
        for name, ww in self.models.items():
            for prob in ww.process_streaming(embeddings[-1]):
                if prob >= self.threshold:
                    return name
        return None

    def wait_for_wake_word(self, timeout_secs: float | None = None,
                           device=None) -> str | None:
        """Block until a wake word is heard on the system mic, then return its name."""
        self.detected.clear()
        self.detected_word = None
        self._stop.clear()

        def cb(indata, frames, t_info, status):
            if status:
                print(f"!! audio status: {status}", file=sys.stderr)
            if self._stop.is_set() or self.detected.is_set():
                return
            audio = np.frombuffer(indata, dtype=np.int16).tobytes()
            word = self._process_chunk(audio)
            if word:
                self.detected_word = word
                self.detected.set()
                self._stop.set()
                print(f">> wake word: {word}", file=sys.stderr)

        with sd.InputStream(samplerate=self.sample_rate, channels=1,
                            dtype="int16", device=device,
                            callback=cb, blocksize=1280):
            self.detected.wait(timeout=timeout_secs)
        return self.detected_word

    def close(self) -> None:
        for ww in self.models.values():
            ww.close()


class CommandMode:
    """Wake word → record utterance → transcribe → handle command."""

    def __init__(self, config: dict, tts: Transcriber):
        self.cfg = config
        ww_cfg = config.get("wake_word", {})
        stt_cfg = config.get("stt", {})
        cmd_cfg = config.get("command", {})

        names = ww_cfg.get("models", ["jarvis", "computer"])
        self.engine = WakeWordEngine(
            model_names=names,
            threshold=ww_cfg.get("threshold", 0.5),
            sample_rate=ww_cfg.get("sample_rate", 16000),
        )
        self.sample_rate = ww_cfg.get("sample_rate", 16000)
        self.listen_timeout = cmd_cfg.get("listen_timeout_secs", 6)
        self.min_secs = cmd_cfg.get("min_secs", 0.4)
        self.language = stt_cfg.get("language", "en")
        self.device = ww_cfg.get("device")
        self.stt = tts
        self.handler = CommandHandler(config)

    def _record_utterance(self, max_secs: float) -> bytes:
        """Record from system mic until silence or max_secs. Returns PCM16 bytes."""
        print(">> listening for command…", file=sys.stderr)
        chunks: list[np.ndarray] = []
        silence_secs = 0.0
        frame_secs = 0.08
        start = time.monotonic()

        def cb(indata, frames, t_info, status):
            nonlocal silence_secs
            if status:
                print(f"!! audio status: {status}", file=sys.stderr)
            audio = np.frombuffer(indata, dtype=np.int16).flatten()
            chunks.append(audio.copy())
            rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)) / 32768.0)
            if rms < 0.02:
                silence_secs += frame_secs
            else:
                silence_secs = 0.0

        with sd.InputStream(samplerate=self.sample_rate, channels=1,
                            dtype="int16", device=self.device,
                            callback=cb, blocksize=1280):
            while time.monotonic() - start < max_secs:
                if silence_secs > 1.5 and len(chunks) > 5:
                    break
                time.sleep(0.05)

        if not chunks:
            return b""
        return np.concatenate(chunks).astype(np.int16).tobytes()

    def run_once(self) -> None:
        """One wake-word → command cycle."""
        word = self.engine.wait_for_wake_word(timeout_secs=None,
                                              device=self.device)
        if not word:
            print(">> wake-word listener stopped.", file=sys.stderr)
            return

        # Stop wake-word listening while we record (one mic at a time).
        self.engine._stop.set()

        pcm = self._record_utterance(self.listen_timeout)
        if not pcm:
            print(">> no audio captured", file=sys.stderr)
            self.engine._stop.clear()
            return

        duration = len(pcm) / 2 / self.sample_rate
        if duration < self.min_secs:
            print(f">> utterance too short ({duration:.1f}s)", file=sys.stderr)
            self.engine._stop.clear()
            return

        text = self.stt.transcribe_pcm16(pcm, self.sample_rate)
        if not text:
            print(">> no speech detected.", file=sys.stderr)
            self.engine._stop.clear()
            return

        print(f">> transcript: {text}", file=sys.stderr)
        print(self.handler.handle(text), file=sys.stderr)
        self.engine._stop.clear()

    def run_forever(self) -> None:
        print(">> command mode: waiting for wake word (Ctrl-C to exit)…",
              file=sys.stderr)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\n>> stopping", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                print(f"!! command error: {e}", file=sys.stderr)
                time.sleep(1)
        self.engine.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Wake-word voice command daemon.")
    ap.add_argument("--wake-only", action="store_true",
                    help="only listen for wake word, print it and exit")
    ap.add_argument("--forever", action="store_true",
                    help="keep listening for wake words (repeat command cycle)")
    ap.add_argument("--config", help="path to voice_assistant.yaml override")
    args = ap.parse_args()

    cfg = load_config()

    tts = build_transcriber(cfg)

    if args.wake_only:
        ww_cfg = cfg.get("wake_word", {})
        engine = WakeWordEngine(
            model_names=ww_cfg.get("models", ["jarvis", "computer"]),
            threshold=ww_cfg.get("threshold", 0.5),
            sample_rate=ww_cfg.get("sample_rate", 16000),
        )
        word = engine.wait_for_wake_word()
        if word:
            print(f">> wake word detected: {word}")
        else:
            print(">> no wake word detected")
        engine.close()
        return

    mode = CommandMode(cfg, tts)
    if args.forever:
        mode.run_forever()
    else:
        mode.run_once()


if __name__ == "__main__":
    main()