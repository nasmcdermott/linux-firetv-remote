# Changelog

All notable changes are recorded here. This project starts with a single
squashed release; earlier private development is summarized in
[docs/MIGRATION.md](docs/MIGRATION.md).

## Unreleased

### Fixed

- Survive distro Python upgrades: `scripts/ensure_venv.py` runs from
  `install.sh`, both systemd services and the CLI launcher. It detects a stale
  or incomplete `.venv`, rebuilds it, and falls back to a `uv`-managed
  interpreter only when the system Python ABI has no wheels for the voice
  stack.
- BLE daemons auto-detect the Amazon Fire TV remote by HID vendor instead of
  requiring an exact MAC, so Menu and voice keep working after re-pairing or
  an upgrade changes the bluetooth address.
- Reinstalling without `REMOTE_MAC` no longer overwrites a real remote address
  in the systemd units with the placeholder.
- Native Python 3.15 support for the base voice path: `ctranslate2` is updated
  to 4.9.0 (cp315 wheels), the wake-word dependencies are now an optional
  extra, and `onnxruntime` is excluded on Python 3.15+ because it is only used
  for faster-whisper's optional VAD filter. Voice falls back to no-VAD when it
  is absent.
- Wake-word support (`07_wake_word.py`) is now an optional `wakeword` extra
  (`uv sync --extra wakeword`); it still needs Python 3.11-3.14 until
  `onnxruntime` publishes cp315 wheels.
- `voice-stt` binds WSS 9090 before loading Whisper, so the receiver reliably
  delegates phone voice to the shared model instead of racing it with a
  second copy.

### Added

- `scripts/ensure_venv.py`, shared by the installer, services and CLI, so the
  installed application repairs its own environment instead of requiring a
  reinstall after a system change.

## 1.0.0 — 2026-11-08

First public release.

### Added

- Physical Amazon Fire TV BLE remote support: D-pad/select/back, home, menu,
  media, app-shortcut buttons, and push-to-talk voice.
- Receiver for the official Amazon Fire TV app: SSDP/DIAL discovery, Turnstile
  HTTPS on 8080, app row and icons, keys/media/text, PIN pairing, and WSS 9090
  voice.
- Red “Fire TV Remote” PIN card with KDE layer-shell, GNOME overlay, and X11
  window/terminal fallbacks.
- One shared voice pipeline (`scripts/voice_core.py`) for BLE and phone audio:
  faster-whisper, app vocabulary, and one command router.
- Conservative safe-command gate: only short, explicit commands with a known
  app target execute; longer sentences, clauses, and embedded “open …” are
  dictated.
- App row actions via the shared launcher (`app:`), desktop entries
  (`desktop:`), and configured commands.
- Paired-device store with `firetv-remote devices` / `forget`.
- `firetv-remote` CLI; `firetv-receiver` compatibility symlink.
- Requirements/prerequisite matrix with hardware, permissions, network,
  disk/model sizes, and a pre-flight checklist (`docs/REQUIREMENTS.md`).
- Portable installer: KDE and non-KDE desktops, CEC or local media keys,
  systemd or foreground mode, uv or venv+pip, optional udev fixes.
- `--ble-only`, `--phone-only`, `--dry-run`, `--uninstall` and documented
  advanced flags. `--force-keymap` keeps user app rows.
- Android client-side trust-bypass notes for the stock app's pinned Turnstile
  certificate.

### Changed

- Voice transcription is shared instead of one copy per input.
- BLE Menu uses the shared action keymap; volume/mute/power move to local media
  keys when CEC is not present.
- Installer no longer assumes Fedora, KDE, systemd, or uv, and never runs
  `sudo`; optional udev commands are printed (`FIRETV_INSTALL_UDEV=1` opts in).

### Fixed

- Home sends `KEY_HOMEPAGE`; Power wakes via CEC then sends the power sequence.
- Volume/mute capability flags are advertised correctly.
- Tokens and pairing state persist across restarts.
- Phone voice and BLE voice no longer compete for port 9090.
