# Linux Fire TV Remote

Control a Linux media PC with both:

- the **physical Amazon Fire TV 4K Alexa Voice Remote** over Bluetooth LE, and
- the **official Amazon Fire TV app** on iPhone, iPad or Android over the LAN.

Both inputs share one local voice pipeline: faster-whisper transcription, a
conservative command/dictation gate, and a single command router. No cloud,
no accounts or API keys to configure, no third-party remote clones.

```
BLE remote ── hidraw/Opus ─┐
                           ├─ voice_core.py ── faster-whisper ── CommandHandler
phone app ── WSS 9090 PCM ─┘                                   ├─ launch app
                                                               ├─ search file
                                                               └─ type / dictate
```

This is an independent fork of
[`justanotheruseer/fire-remote-client`](https://github.com/justanotheruseer/fire-remote-client).
The original project reverse-engineered the BLE Fire TV Stick 4K Alexa Voice
Remote; its BLE capture, Opus voice pipeline, command handler, and
service/install patterns remain the base of this tree. It is MIT-licensed and
credited in [`LICENSE`](LICENSE), [`CONTRIBUTORS.md`](CONTRIBUTORS.md) and
[`docs/UPSTREAM.md`](docs/UPSTREAM.md).

This fork adds the official Amazon Fire TV app receiver (DIAL/HTTPS/WSS), the
GUI pairing-PIN card, and one shared voice/command pipeline for both inputs.
It is not affiliated with Amazon or with the upstream author.

## Why this exists

The official Amazon Fire TV app only discovers and pairs with Fire TV devices.
This project makes a Linux media PC answer that protocol, so the phone app can
control it. BLE remote support and the shared voice pipeline exist so the same
box works with either input; the receiver is the reason the project exists.

> **Best-effort, no warranty.** This is reverse-engineered software, not an
> Amazon product. The protocol can change without notice, and the stock Android
> app pins Amazon's certificate. It is provided **“as is”, without warranty of
> any kind, express or implied**. Use at your own risk; see [`LICENSE`](LICENSE).

## Features

- **BLE remote**: D-pad, select, back, home, menu, media, app-shortcut buttons,
  volume/mute/power, and push-to-talk microphone.
- **Official phone app**: appears in “Connect to Fire TV”, pairs with a red PIN
  card, and sends keys, text, app launches and microphone audio.
- **One voice model**: the BLE button and the phone microphone share a single
  faster-whisper model and `CommandHandler` in `voice-stt.service`.
- **Safe commands**: only short, explicit commands with a known app target
  execute. “Open Firefox” works; “I want to open Firefox because …” is
  dictated, not executed.
- **PIN card**: red “Fire TV Remote” Qt card, top-right on KDE Wayland, with
  GNOME/X11 fallbacks.
- **App row**: configurable names, icons and launch actions.
- **Interactive remapping**: `firetv-remote remap` captures a physical button and
  maps it to an app, file, or shell command.
- **Device memory**: paired phones are stored with `devices.json`; list and
  forget them from the CLI.
- **Portable defaults**: KDE and non-KDE desktops; CEC or local media keys;
  systemd or foreground mode; uv or venv+pip.
- **Upgrade-safe venv**: `scripts/ensure_venv.py` checks and rebuilds the
  project venv at service/CLI startup, so a distro Python upgrade does not
  leave voice broken.
- **No root**: installs under `$HOME` and the checkout. `sudo` is never run;
  optional udev commands are printed for you.

## Requirements

- Linux with Python 3.11+, BlueZ for the BLE remote, and `libopus`.
- A writable `/dev/uinput` for `ydotool`, or `wtype`/`xdotool`.
- A Wayland or X11 session for typing and the PIN card; TTY is enough for keys.
- Same LAN as the phone, with SSDP multicast allowed; no internet after the
  Whisper model is cached.

The full matrix — hardware, distro packages, permissions, ports, disk and
model sizes, and a pre-flight checklist — is in
[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md).

## Quick start

```bash
git clone https://github.com/nasmcdermott/linux-firetv-remote.git
cd linux-firetv-remote
./install.sh
firetv-remote doctor
firetv-remote pair
```

The installer detects a connected Amazon remote automatically; `REMOTE_MAC` is
only needed to choose one when several are connected. The BLE daemons also
re-discover the remote after a re-pair, so a changed Bluetooth address does not
break the Menu button or voice.

The installer writes only under `$HOME` and this checkout, creates a project
`.venv` if needed, installs systemd user units when available, and enables
them at login.
It detects KDE vs other desktops and CEC ownership, writing
`config/keymap.json` only when portable overrides are needed.

On the phone: open the official **Amazon Fire TV** app → **Connect to Fire TV**
→ select the machine name → enter the PIN from the card or `firetv-remote pin`.

Useful commands:

```bash
firetv-remote status
firetv-remote devices
firetv-remote forget "iPhone"
firetv-remote preview-pin --ttl 5
firetv-remote keys
firetv-remote remap                    # press a button, map it to a command/app
firetv-remote run -v                  # foreground receiver
journalctl --user -u firetv-receiver -f
journalctl --user -u voice-stt -f
```

The legacy `firetv-receiver` command is installed as a compatibility symlink.

### Updating

The installer is idempotent and keeps existing pairings and user config. To
update a checkout:

```bash
git pull --ff-only
./update.sh          # re-runs install.sh; add --pull to fetch first
```

`./uninstall.sh` removes the services and launcher; add `--purge` to also delete
pairings, TLS identity and the project venv.

## Desktop and distro support

The bridge targets any modern Linux desktop and avoids hard dependencies on
KDE, Fedora, or a specific init system.

| Area | Portable behavior |
|---|---|
| Desktop | KDE uses Plasma D-Bus/`KEY_HOMEPAGE`; other desktops use `Menu`/`Super` key overrides |
| PIN card | KDE layer-shell, GNOME fullscreen, X11/other top-right window; terminal fallback |
| App launch | `CommandHandler` uses kstart5, `gtk-launch`, `gio launch`, `xdg-open`, `wmctrl` |
| D-Bus | `busctl`, then `gdbus`, then `dbus-send` |
| Volume/power | CEC when `cec-remote-observer` owns it; otherwise local media keys via `--portable` |
| Init | systemd user units; `--no-service` prints foreground commands for OpenRC/runit/etc. |
| Python | `uv` if available; otherwise `python3 -m venv` + `pip` |

See [`docs/INSTALL.md`](docs/INSTALL.md) and [`docs/DESKTOP.md`](docs/DESKTOP.md)
for the distro/desktop matrix and package names.

## Configuration

- `config/keymap.example.json` — receiver/phone actions and app row (copy to
  `config/keymap.json`). The installer generates a portable override when
  needed.
- `config/voice_assistant.yaml` — Whisper model, hotwords, command patterns,
  `safe_commands`, Waydroid options. Local overrides go in
  `config/voice_assistant.local.yaml`.
- `config/voice.json` — WSS transport mode (`command`, `dictation`, `log`),
  WAV saving and the phone-mic port.
- `config/button_launch.yaml` — local physical-remote button mappings
  (gitignored; the installer copies it from
  `config/button_launch.example.yaml`, and `firetv-remote remap` edits it).
- `~/.config/firetv-receiver/config.json` — receiver bind/ports/PIN display.
- `~/.config/firetv-receiver/devices.json` — paired phones.

## Voice safety

`safe_commands: true` in `config/voice_assistant.yaml` enables a conservative
gate:

- short explicit commands with a known app target execute;
- long sentences, clause words, internal punctuation, unknown app names, and
  “open …” embedded in dictation are typed instead.

Examples: “Open Firefox” → launch; “Open Firefox and tell me the weather” →
dictation; “I want to open Firefox” → dictation; “Open NotAnApp” → dictation.

## Android

The stock Android app pins Amazon’s embedded Turnstile certificate. The private
key is not in the APK, so there is no Linux-side certificate workaround. The
official **iOS** app works with the receiver’s self-signed certificate. For
Android, see [`tools/android/README.md`](tools/android/README.md) for the
Frida/LSPosed/patched-APK client-side bypass and Waydroid caveats.

## Documentation

| Document | Contents |
|---|---|
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | hardware, software, permissions, ports, pre-flight checklist |
| [`docs/INSTALL.md`](docs/INSTALL.md) | distro packages, install/update/uninstall, non-systemd |
| [`docs/DESKTOP.md`](docs/DESKTOP.md) | KDE/GNOME/X11/Wayland behavior and overrides |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | components, ports, services, data flow |
| [`docs/VOICE.md`](docs/VOICE.md) | BLE + phone voice, shared core, safe commands |
| [`docs/RECEIVER.md`](docs/RECEIVER.md) | official app protocol, PIN, app row, devices |
| [`docs/BUTTONS.md`](docs/BUTTONS.md) | BLE buttons, Menu, volume/power, remapping |
| [`docs/MIGRATION.md`](docs/MIGRATION.md) | migrating from the original projects |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | common failures and fixes |
| [`docs/UPSTREAM.md`](docs/UPSTREAM.md) | every upstream project used and its license |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | development, tests, release process |
| [`CHANGELOG.md`](CHANGELOG.md) | release summary |
| [`SECURITY.md`](SECURITY.md) | LAN trust model, tokens, reporting |

## What is original vs borrowed

Original to this project:

- the DIAL/HTTPS/WSS receiver, PIN flow, app row, token store, and phone voice
  transport;
- the shared BLE + phone voice/command pipeline and safe-command gate;
- the Fire TV-specific PIN-card adaptation, portability work, installer, and
  tests.

Adapted from MIT-licensed upstreams:

- BLE capture, Opus voice pipeline, and command handler:
  [`justanotheruseer/fire-remote-client`](https://github.com/justanotheruseer/fire-remote-client);
- PIN-card code and design:
  [`linux-apple-remote`](https://github.com/nasmcdermott/linux-apple-remote).

Full provenance and licenses: [`NOTICE.md`](NOTICE.md),
[`CONTRIBUTORS.md`](CONTRIBUTORS.md), and [`docs/UPSTREAM.md`](docs/UPSTREAM.md).

## Development note

This project was vibe-coded with a mix of **DeepSeek 4.1** and **Claude 5.5**
models, with the author directing the architecture, protocol research, hardware
testing, and release review. AI-assisted code gets no special trust: review it,
run the tests, and treat it under the no-warranty terms above.

## License and credits

MIT. See [`LICENSE`](LICENSE), [`NOTICE.md`](NOTICE.md),
[`CONTRIBUTORS.md`](CONTRIBUTORS.md), and [`docs/UPSTREAM.md`](docs/UPSTREAM.md).

The BLE pipeline is based on
[`justanotheruseer/fire-remote-client`](https://github.com/justanotheruseer/fire-remote-client),
Copyright (c) 2026 justanotheruseer. The PIN card is adapted from the custom
PIN-card implementation in
[`linux-apple-remote`](https://github.com/nasmcdermott/linux-apple-remote),
MIT-licensed. It is not derived from `windows-apple-remote`; see
[`NOTICE.md`](NOTICE.md) for the transitive license note.
The official-app receiver's protocol work was cross-checked against
[`Edx0n/firetv-remote-control-adfree`](https://github.com/Edx0n/firetv-remote-control-adfree),
[`LibreControl/fire-tv`](https://github.com/LibreControl/fire-tv),
[`pyfiretvremote`](https://pypi.org/project/pyfiretvremote/),
[`hms-homelab/hms-firetv`](https://github.com/hms-homelab/hms-firetv), the
Home Assistant Fire TV REST-command community, and a public Smart TV Emulator
project. BLE voice details were cross-checked against
[`Staars/berry-examples`](https://github.com/Staars/berry-examples). No code or
assets from these projects are included. The full table, including runtime
dependencies and licenses, is in [`docs/UPSTREAM.md`](docs/UPSTREAM.md). No
Amazon code, certificate, private key, or firmware is included.
