#!/usr/bin/env python3
"""Phase 6: transcribe a decoded voice WAV to text (offline).

Takes the 16 kHz mono WAV produced by 04_decode_opus.py and runs it through
faster-whisper locally — no network, no API keys. The model downloads once
(~75 MB for `tiny`, ~145 MB for `base`) and is cached in ~/.cache/huggingface.

Usage:
    uv run python scripts/05_transcribe.py captures/voice_<utc>.wav
    uv run python scripts/05_transcribe.py captures/voice_<utc>.wav --model small --language en
    uv run python scripts/05_transcribe.py out.wav --out out.txt   # save sidecar

Pairs with the push-to-talk flow: hold the remote's voice button (03_capture
or poc), release, decode (04), transcribe (this script). For one command that
does decode+transcribe, use: just transcribe captures/voice_<utc>.bin
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="WAV from 04_decode_opus.py (16 kHz mono)")
    ap.add_argument("--model", default="base",
                    help="faster-whisper model: tiny/base/small/medium (default: base)")
    ap.add_argument("--language", default="en", help="language code (default: en)")
    ap.add_argument("--out", help="write transcript to FILE as well as stdout")
    ap.add_argument("--device", default="cpu", help="compute device (default: cpu)")
    ap.add_argument("--compute-type", default="int8",
                    help="quantization for CPU (default: int8, fastest)")
    ap.add_argument("--hotwords", default=None,
                    help="comma-separated hotwords to bias recognition (e.g. 'Steam, Konsole')")
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        raise SystemExit(f"Not found: {inp}")
    try:
        secs = wav_duration(inp)
    except wave.Error as e:
        raise SystemExit(f"Not a valid WAV: {inp} ({e})") from e
    if secs < 0.3:
        print(f"!! only {secs:.2f}s of audio — hold the voice button longer. "
              f"Transcribing anyway.", file=sys.stderr)

    import faster_whisper.audio as fw_audio
    from faster_whisper import WhisperModel

    # Compat: faster-whisper 1.2 calls av.open(metadata_errors="ignore"),
    # a kwarg PyAV>=15 removed. Our WAVs are clean PCM so dropping it is safe.
    _av_open = fw_audio.av.open

    def _open_compat(file, *args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _av_open(file, *args, **kwargs)

    fw_audio.av.open = _open_compat

    print(f">> loading faster-whisper '{args.model}' (first run downloads it)…",
          file=sys.stderr)
    model = WhisperModel(args.model, device=args.device,
                         compute_type=args.compute_type)
    kwargs = dict(language=args.language, beam_size=5)
    if args.hotwords:
        kwargs["hotwords"] = args.hotwords
    segments, info = model.transcribe(str(inp), **kwargs)
    text = " ".join(s.text.strip() for s in segments).strip()
    print(f">> detected language: {info.language} "
          f"(p={info.language_probability:.2f}), {secs:.2f}s audio",
          file=sys.stderr)
    if not text:
        print(">> no speech detected.", file=sys.stderr)
        return
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f">> wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
