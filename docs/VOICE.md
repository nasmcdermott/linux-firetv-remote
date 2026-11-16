# Voice

Both voice inputs end as 16 kHz mono s16le PCM and go through
`scripts/voice_core.py`.

| Input | Capture | Notes |
|---|---|---|
| BLE remote | hidraw report `0xF0`, Opus 20 ms frames | decoded with `opuslib` |
| Phone app | WSS 9090 binary frames | raw PCM; no container |

`voice-stt.service` runs with `--wss`, so one process owns both inputs and one
Whisper model. If the receiver runs without `voice-stt` (for example
`--phone-only`), it starts a standalone WSS and loads its own model.

## Safe commands

`config/voice_assistant.yaml`:

```yaml
command:
  safe_commands: true
```

With the gate enabled:

- “Open Firefox” → launch;
- “Open Firefox.” → launch (trailing punctuation is ignored);
- “Open Firefox and tell me the weather” → dictation;
- “I want to open Firefox because …” → dictation;
- “Open NotAnApp” → dictation (unknown target, never a shell command);
- long sentences containing “open …” → dictation.

The gate is implemented by `voice_core.classify_utterance()` and covered by
`tests/test_voice_safety.py`. The same decision runs for BLE and phone input.

`config/voice.json` chooses the phone transport mode:

```json
{
  "mode": "command",        // command | dictation | log
  "safe_commands": true,
  "save_audio": true,
  "min_seconds": 0.3
}
```

In the merged install, the voice-stt daemon handles the phone WSS and uses the
`command`/safe policy from `voice_assistant.yaml`; the `mode` field applies to
the standalone receiver WSS.

## Models

`config/voice_assistant.yaml`:

```yaml
stt:
  model: small
  language: en
  device: cpu
  compute_type: int8
  hotwords: "Steam, Konsole, Firefox, Kodi, Waydroid"
  initial_prompt: "Hello, world. How are you? I'm fine - thanks. Use 1, 2, 3."
```

Use `tiny`/`base` for speed, `medium`/`large-v3` for accuracy. Models are cached
by faster-whisper under `~/.cache/huggingface/`.

## Dictation

The fallback command pattern types anything that is not a confident command:

```yaml
- regex: "^open\\s+(.+)$"
  action: launch_app
- regex: "^search\\s+for\\s+(.+)$"
  action: search_file
- regex: "^(type|write|dictate)\\s+(.+)$"
  action: type_text
- regex: "^.+$"
  action: type_text
```

`08_command_handler.type_text()` uses `ydotool` first, then `wtype`, then
`xdotool`. It normalizes spoken punctuation (for example “comma” → `,`) and
strips trailing Whisper punctuation.

## VAD and the optional wake word

faster-whisper uses `onnxruntime` for its optional voice-activity filter. The
base Fire TV remote path does not require `onnxruntime`: push-to-talk already
bounds each utterance, and `voice_core` runs Whisper without the VAD filter
when the module is unavailable. This keeps the base install working on newer
Python ABIs (for example cp315).

The always-listening wake word (`scripts/07_wake_word.py`) is an optional
extra: `uv sync --extra wakeword`. It currently needs Python 3.11-3.14 until
`onnxruntime` publishes cp315 wheels.

## One daemon only

`voice-stt.service` and `voice-assistant.service` are mutually exclusive: each
would transcribe and type every utterance. Enable one. The merged setup uses
`voice-stt.service`; it now also serves the phone WSS.

## Tests

```bash
python -m unittest tests.test_voice_safety tests.test_remote_input
```
