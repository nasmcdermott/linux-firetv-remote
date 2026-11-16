#!/usr/bin/env python3
"""Shared faster-whisper STT + command routing for both voice paths.

The BLE remote (``06_voice_stt.py`` / ``voice_assistant.py``) and the official
phone app's WSS stream both end up as 16 kHz mono s16le PCM. This module owns
the one Whisper model and the one command router so both paths behave the same:

  * ``open firefox`` / ``search for notes`` -> the existing CommandHandler
  * anything else -> dictation typed by the existing CommandHandler

The receiver uses this directly; the BLE scripts can adopt it incrementally
without changing their hidraw/Opus capture code.
"""

from __future__ import annotations

import difflib
import importlib
import re
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Conservative voice-command gate. A transcript is only executed as a command
# when it is short, starts with an explicit verb, has no clause words, and (for
# "open"/"launch") the target is a known app. Everything else is dictation, so
# "please open Firefox and ..." never launches anything mid-sentence.
_COMMAND_RE = re.compile(r"^(?:open|launch|start)\s+(.+)$", re.I)
_SEARCH_RE = re.compile(r"^(?:search|find)\s+(?:for\s+)?(.+)$", re.I)
_TYPE_RE = re.compile(r"^(?:type|write|dictate)\s+(.+)$", re.I)
_CLAUSE_RE = re.compile(
    r"\b(?:and|but|because|so|then|while|when|which|that|please|if|or|"
    r"with|from|to|about|into|over|after|before)\b",
    re.I,
)
_INTERNAL_PUNCT_RE = re.compile(r"[.,;:!?]")
_MAX_COMMAND_WORDS = 6
_MAX_TARGET_WORDS = 4
_FUZZY_CUTOFF = 0.82

# faster-whisper only imports onnxruntime for its optional VAD filter. Keep the
# filter when available, but do not make the whole voice path depend on it:
# onnxruntime can lag new Python ABIs (for example cp315) that ctranslate2 and
# Whisper already support. Push-to-talk already bounds the audio, so Whisper
# handles the utterance without VAD in that case.
try:
    _HAS_ONNXRUNTIME = find_spec("onnxruntime") is not None
except (ImportError, ValueError):
    _HAS_ONNXRUNTIME = False


def command_module() -> Any:
    """Import the numeric-named command handler lazily."""
    return importlib.import_module("08_command_handler")


def load_config() -> dict[str, Any]:
    return command_module().load_config()


def _patch_faster_whisper_av() -> None:
    """Compatibility shim for faster-whisper + newer PyAV."""
    try:
        import faster_whisper.audio as fw_audio

        original_open = fw_audio.av.open

        def _open_compat(file: Any, *args: Any, **kwargs: Any) -> Any:
            kwargs.pop("metadata_errors", None)
            return original_open(file, *args, **kwargs)

        fw_audio.av.open = _open_compat
    except Exception:  # noqa: BLE001 - optional compatibility patch
        pass


class Transcriber:
    """faster-whisper wrapper: one model loaded, shared by the caller."""

    def __init__(
        self,
        model: str,
        language: str,
        device: str,
        compute_type: str,
        hotwords: str | None = None,
        initial_prompt: str | None = None,
        beam_size: int = 5,
    ) -> None:
        _patch_faster_whisper_av()
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language
        self.hotwords = hotwords
        self.initial_prompt = initial_prompt
        self.beam_size = beam_size

    def transcribe_pcm16(self, pcm: bytes, rate: int = 16000) -> str:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs: dict[str, Any] = {
            "language": self.language,
            "beam_size": self.beam_size,
            "vad_filter": _HAS_ONNXRUNTIME,
        }
        if self.hotwords:
            kwargs["hotwords"] = self.hotwords
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
        segments, _info = self.model.transcribe(audio, **kwargs)
        cleaned: list[str] = []
        for segment in segments:
            text = segment.text.strip()
            text = re.sub(r"[\s.,!?;:]+$", "", text)
            if text:
                cleaned.append(text)
        return " ".join(cleaned).strip()


def build_transcriber(cfg: dict[str, Any] | None = None) -> Transcriber:
    cfg = cfg or load_config()
    stt = cfg.get("stt", {})
    return Transcriber(
        model=str(stt.get("model", "small")),
        language=str(stt.get("language", "en")),
        device=str(stt.get("device", "cpu")),
        compute_type=str(stt.get("compute_type", "int8")),
        hotwords=stt.get("hotwords") or None,
        initial_prompt=stt.get("initial_prompt") or None,
        beam_size=int(stt.get("beam_size", 5)),
    )


def build_handler(cfg: dict[str, Any] | None = None) -> tuple[Any, Any]:
    """Return (CommandHandler, command module) using the FRC config/vocab."""
    cmd = command_module()
    cfg = cfg or cmd.load_config()
    app_names = cmd.discover_app_names()
    return cmd.CommandHandler(cfg, app_names), cmd


def _known_app(handler: Any, target: str) -> bool:
    """True only when *target* resolves to a real installed app/redirect."""
    target = target.strip()
    if not target:
        return False
    redirects = getattr(handler, "cfg", {}).get("desktop_redirects", {}) or {}
    if target.lower() in redirects:
        return True
    try:
        if handler._find_desktop(target):
            return True
    except Exception:  # noqa: BLE001 - fake/test handlers may not expose it
        pass
    app_names = getattr(handler, "app_names", []) or []
    if app_names:
        names = {str(name).lower(): name for name in app_names}
        matches = difflib.get_close_matches(
            target.lower(), list(names), n=1, cutoff=_FUZZY_CUTOFF
        )
        if matches:
            return True
    return False


def classify_utterance(
    text: str, handler: Any, safe: bool = True
) -> tuple[str, str]:
    """Return ("command"|"dictation", reason).

    Safe mode is deliberately conservative. It only executes the built-in
    launch/search/type commands when the utterance is a short, explicit command
    with a known app target. Long sentences, clause words, internal punctuation
    and unknown app names are dictation. This prevents "open Firefox" embedded
    in a dictated sentence from launching an app.
    """
    if not safe:
        return "command", "safe mode disabled"
    raw = text.strip()
    if not raw:
        return "dictation", "empty"
    cmd = command_module()
    norm = cmd.normalize_transcript(raw)
    words = norm.split()
    if len(words) > _MAX_COMMAND_WORDS:
        return "dictation", f"too long ({len(words)} words)"
    if _INTERNAL_PUNCT_RE.search(norm):
        return "dictation", "sentence punctuation"
    if _CLAUSE_RE.search(norm):
        return "dictation", "clause word"

    match = _COMMAND_RE.match(norm)
    if match:
        target = match.group(1).strip()
        if not target or len(target.split()) > _MAX_TARGET_WORDS:
            return "dictation", "open target too long"
        if not _known_app(handler, target):
            return "dictation", f"unknown app {target!r}"
        return "command", f"open {target}"

    match = _SEARCH_RE.match(norm)
    if match:
        query = match.group(1).strip()
        if not query or len(query.split()) > _MAX_TARGET_WORDS:
            return "dictation", "search query too long"
        return "command", f"search {query!r}"

    if _TYPE_RE.match(norm):
        return "command", "explicit dictation verb"

    return "dictation", "not an explicit command"


def route_text(
    text: str,
    handler: Any,
    mode: str = "command",
    safe: bool = True,
    log: Any = None,
) -> str:
    """Route a transcript through the same handler the BLE voice uses.

    ``mode=dictation`` always types. ``mode=command`` uses the conservative
    classifier above; anything that is not a confident command is typed as
    dictation. ``mode=log`` only prints.
    """
    if not text:
        return "(empty)"
    if mode == "dictation":
        return handler.type_text(text)
    if mode == "log":
        return f"(log only) {text}"

    decision, reason = classify_utterance(text, handler, safe=safe)
    if decision == "dictation":
        if log:
            log(f">> voice guard: {reason}; dictating instead")
        return handler.type_text(text)
    if log:
        log(f">> voice command: {reason}")
    return handler.handle(text)


def describe(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    stt = cfg.get("stt", {})
    return f"faster-whisper {stt.get('model', 'small')} (fire-remote-client)"
