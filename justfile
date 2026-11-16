# Fire TV Voice Remote BLE reverse-engineering pipeline.
# All paths are relative to the repo root. Run `just` to list targets.
#
# Pairing/capture need elevation for btmon and /dev/hidraw* — scripts use doas
# (Alpine) or sudo (Arch), or run them directly under root.

# Remote BLE MAC — set to YOUR remote's MAC (from `just pair` or
# `bluetoothctl devices`). Override per-invocation: just MAC=AA:.. capture
# Or export it once: export REMOTE_MAC=AA:BB:CC:DD:EE:FF
MAC := env_var_or_default("REMOTE_MAC", "AA:BB:CC:DD:EE:FF")
ADAPTER := "hci0"
NAME_RE := "amazon|fire|alexa"

_default:
    @just --list

# Phase 1: guided pairing + HCI capture, then extract & print the LTK.
# Idempotent: re-running after bonding just re-extracts the LTK.
pair:
    REMOTE_MAC={{MAC}} ADAPTER={{ADAPTER}} bash scripts/01_pair_and_capture.sh

# Phase 2: dump the GATT tree (services/chars/flags) -> captures/gatt_enum_<utc>.yaml
enum:
    uv run python scripts/02_enum_gatt.py --mac {{MAC}}

# Phase 3: capture voice audio via hidraw (reactive mic handshake).
# Hold the voice button while it runs. Ctrl-C or --duration to stop.
# Writes captures/voice_<utc>.bin (+ .json sidecar). Needs root.
capture:
    doas python3 scripts/03_capture_voice.py --mac {{MAC}}

# Same capture but buttons only (no mic handshake) — for input mapping.
capture-buttons:
    doas python3 scripts/03_capture_voice.py --mac {{MAC}} --no-enable

# Phase 4/5: decode a captured .bin (Opus -> WAV).
decode FILE:
    uv run python scripts/04_decode_opus.py {{FILE}}

# Phase 6: transcribe a decoded .wav offline (faster-whisper) -> stdout.
stt FILE:
    uv run python scripts/05_transcribe.py {{FILE}}

# Push-to-talk in one command: .bin -> .wav -> text.
transcribe FILE:
    uv run python scripts/04_decode_opus.py {{FILE}}
    uv run python scripts/05_transcribe.py "$(echo '{{FILE}}' | sed 's/\.[^.]*$/.wav/')"

# Push-to-talk daemon: hold voice -> speak -> release -> command or type.
# Routes through 08_command_handler: commands execute, dictation gets typed.
live:
    uv run python scripts/06_voice_stt.py --mac {{MAC}} --type --model small

# Handle a single transcript: "open firefox" | "search for my notes" | "type hello world".
command TEXT:
    uv run python scripts/08_command_handler.py {{TEXT}}

# Unified daemon: remote-driven only (voice button = trigger). No system mic.
assistant:
    uv run python scripts/voice_assistant.py --mac {{MAC}}

# Decode the button presses in a capture into a labeled table.
buttons FILE:
    uv run python scripts/decode_buttons.py {{FILE}}

# Run the button -> app launcher daemon in the foreground.
launch:
    python3 scripts/remote_launch.py --config config/button_launch.yaml

# All-in-one PoC: live button mapping + reactive voice capture -> WAV.
# Press buttons to see them named; hold voice to record. Ctrl-C to stop.
poc:
    doas python3 scripts/poc_fire_voice.py --mac {{MAC}}

# Official Fire TV app receiver (DIAL/HTTPS + PIN card + WSS voice).
receiver:
    uv run python -m receiver.cli run -v

receiver-doctor:
    uv run python -m receiver.cli doctor

receiver-pair:
    uv run python -m receiver.cli pair

devices:
    uv run python -m receiver.cli devices

forget NAME:
    uv run python -m receiver.cli forget {{NAME}}
