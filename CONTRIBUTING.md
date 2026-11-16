# Contributing

Thanks for helping. This project is a merged Linux implementation: a BLE Fire TV
remote pipeline plus a receiver for the official Amazon Fire TV app. Keep
changes focused and portable across desktops and distros.

## Layout

```text
install.sh, uninstall.sh     user-level installer (no root except optional udev)
scripts/                     BLE capture, voice daemon, command router, launchers
receiver/                    DIAL/HTTPS/WSS receiver, PIN card, CLI, action layer
config/                      tracked examples and defaults
systemd/                     user units
udev/                        optional hidraw and input-layer fixes
tests/                       unittest suite
docs/                        user and design documentation
tools/android/               Android trust-bypass notes and helper script
```

## Development setup

Python 3.11+ is required. Use whichever environment you prefer:

```bash
# uv (preferred by the installer when available)
uv sync
uv run python -m unittest discover -s tests -v

# or plain venv + pip
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Run the receiver in the foreground while developing:

```bash
.venv/bin/python -m receiver.cli run -v
```

The Qt PIN card needs PySide6; without it the PIN is printed in the log and
`firetv-remote pin` still works.

## Checks before a PR

```bash
uv run ruff check .
uv run python -m unittest discover -s tests -v
bash -n install.sh uninstall.sh verify.sh
```

If you touch the BLE button path, run `tests/test_remote_input.py`. If you touch
voice routing or the command gate, add cases to `tests/test_voice_safety.py`.

## Design rules

- **One voice pipeline.** BLE and phone audio go through
  `scripts/voice_core.py`. Do not add a second transcriber or command router.
- **Safe by default.** A spoken command executes only when it is short, has a
  known app target, and is not embedded in a longer sentence. Everything else
  is dictation. See `classify_utterance()`.
- **User-level install.** Never write outside `$HOME` and the checkout unless
  the user explicitly accepts an optional udev/hwdb rule.
- **Portable choices.** Prefer fallbacks (`gtk-launch` → `gio` → `xdg-open`,
  `busctl` → `gdbus` → `dbus-send`, uv → venv) over hard dependencies on KDE,
  Fedora, systemd, or one launcher.
- **No secrets.** Do not commit MAC addresses, hostnames, LAN IPs, tokens,
  TLS private keys, pairing databases, or raw captures. See
  [SECURITY.md](SECURITY.md).

## Adding apps or buttons

- App row: edit `config/keymap.example.json` and document the shape in
  `docs/RECEIVER.md`. Use `app:<name>` for the shared launcher and
  `desktop:<id>` for a `.desktop` entry.
- Physical button: edit `config/button_launch.yaml`; the action may be a
  `.desktop` file, a file/directory, or a command.
- Keymap: put local overrides in `config/keymap.json` (gitignored). Built-in
  defaults must stay sane without it.

## Commit and release style

- Keep commits small and descriptive. This repository ships a squashed initial
  release so the public history stays readable; internal iteration stays out.
- Update `CHANGELOG.md` for user-visible changes.
- Before tagging, run the full check list above and a PII scan:

```bash
grep -rniE '([0-9a-f]{2}:){5}[0-9a-f]{2}|/home/[a-z0-9_-]+|token|BEGIN .*PRIVATE KEY' \
  --exclude-dir=.git --exclude-dir=.venv .
```

False positives in documentation placeholders (`AA:BB:CC:DD:EE:FF`) are fine.

## Attribution

New code is MIT. Keep upstream credit intact in `NOTICE.md`,
`CONTRIBUTORS.md`, and `docs/UPSTREAM.md`. The PIN card design credit belongs
to `linux-apple-remote` only.
