# Migration from the original projects

This project is a new, merged tree. It does not carry the original commit
history. The history is intentionally a single release commit; this document is
the human-readable summary.

## From `fire-remote-client`

Kept:

- BLE pairing/capture/Opus decode scripts and the physical remote pipeline.
- `voice-stt.service`, `remote-input.service`, `remote-launch.service`.
- `config/voice_assistant.yaml`, `config/button_launch.yaml`.
- the existing voice/command configuration and app vocabulary logic.

Changed:

- Voice transcription now lives in `scripts/voice_core.py`; both BLE and phone
  mic use it.
- `remote_input.py` Menu now goes through the shared `receiver/actions.py`
  keymap. Without CEC, `--portable` handles volume/mute/power locally.
- `firetv-remote` is the new CLI; `firetv-receiver` is a compatibility symlink.
- New service: `firetv-receiver.service` for the official phone app.
- Installer detects KDE/non-KDE, CEC/no-CEC, systemd/no-systemd, and uv/no-uv.

## From `firetv-receiver`

Kept:

- `~/.config/firetv-receiver/config.json`
- `~/.config/firetv-receiver/devices.json` (old `tokens.json` is migrated once)
- `~/.config/firetv-receiver/poc-tls/`
- the red PIN card, app row/icons, device store and receiver CLI.

Changed:

- The phone WSS 9090 stream is served by `voice-stt.service` in the merged
  install, using the same model and command router as the BLE remote.
- `firetv-receiver` command → `firetv-remote` (compat symlink installed).
- Receiver code moved from `poc/` to `receiver/`.
- The receiver’s old standalone Transcriber was removed in favor of
  `scripts/voice_core.py`.

## Config compatibility

| Old | New |
|---|---|
| `poc/turnstile_poc.py` | `python -m receiver.cli run` / `firetv-remote run` |
| `~/.config/firetv-receiver/poc-tls/tokens.json` | migrated to `devices.json` |
| `poc/turnstile_poc.py --inject ydotool` | `install.sh` + services, or `firetv-remote run` |
| `firetv-receiver ...` | `firetv-remote ...` (same commands) |

## Release history

The release commit summarizes the whole merge. Earlier work exists in the
private development trees and in the upstream projects listed in
`docs/UPSTREAM.md`; it is not carried into this repository to avoid noisy
intermediate commits.
