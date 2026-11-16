# Upstream projects and credits

This project is an independent Linux implementation. It does not include
Amazon code, certificates, private keys or firmware.

## Code bases used or adapted

| Project | What we used | License |
|---|---|---|
| [`justanotheruseer/fire-remote-client`](https://github.com/justanotheruseer/fire-remote-client) | BLE pairing/capture, Opus voice pipeline, command handler, service/install patterns | MIT |
| [`nasmcdermott/linux-apple-remote`](https://github.com/nasmcdermott/linux-apple-remote) | PIN-card implementation, palette/desktop-color approach, layer-shell/window launcher | MIT |

The PIN-card implementation in this repository is adapted from
`linux-apple-remote`'s own custom PIN card. It is not derived from
`windows-apple-remote`, which has no PIN UI and contributed only the Apple TV
Companion server in `linux-apple-remote` (not used here). `linux-apple-remote`'s
LICENSE carries a derivation notice for `windows-apple-remote` by Arya Kavian
(MIT), so that text is kept in [`LICENSES/`](../LICENSES/) for completeness;
no `windows-apple-remote` code is included.

## Protocol references (no code copied)

| Project | What we cross-checked |
|---|---|
| [`Staars/berry-examples`](https://github.com/Staars/berry-examples) | BLE voice enable/HID/Opus reference |
| [`Edx0n/firetv-remote-control-adfree`](https://github.com/Edx0n/firetv-remote-control-adfree) | Static analysis of the official Android app 4.9.1: DIAL, Turnstile endpoints, PIN flow, app list/icons, keyboard, voice |
| [`LibreControl/fire-tv`](https://github.com/LibreControl/fire-tv) | Discovery, pairing, keys/media/text/apps/voice reference implementation |
| [`pyfiretvremote`](https://pypi.org/project/pyfiretvremote/) | Async client reference for pairing and voice |
| [`hms-homelab/hms-firetv`](https://github.com/hms-homelab/hms-firetv) | Turnstile endpoints and WSS 9090 voice notes |
| Smart TV Emulator (public project) | Existence proof that the stock iOS app can drive a software Fire TV |
| Home Assistant community: REST commands for Fire TV | Independent confirmation of the public API key, endpoints and headers |

These projects informed the protocol facts only. Their code and assets are not
copied into this repository.

## Runtime dependencies

| Dependency | Role | License |
|---|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | local speech-to-text | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | Whisper inference backend | MIT |
| [PyAV](https://github.com/PyAV-Org/PyAV) | audio decode | BSD-3-Clause |
| [NumPy](https://numpy.org/) | audio buffers | BSD-3-Clause |
| [PyYAML](https://pyyaml.org/) | configuration | MIT |
| [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast) | BlueZ over D-Bus | MIT |
| [opuslib](https://github.com/ornicar/opuslib) | Opus decode binding | BSD-style (see upstream) |
| [pyopen-wakeword](https://github.com/fquirin/pyopen-wakeword) | optional wake word | see upstream |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | optional system microphone | MIT |
| [PySide6 / Qt 6](https://www.qt.io/) | PIN card | LGPL-3.0 / GPL / commercial |
| [KDE layer-shell-qt](https://invent.kde.org/plasma/layer-shell-qt) | KDE Wayland PIN card | LGPL |
| [ydotool](https://github.com/ReimuNotMoe/ydotool) | key/text injection | GPL-2.0 (separate executable) |
| [BlueZ](https://www.bluez.org/) | Bluetooth stack | GPL-2.0 (system component) |
| [systemd](https://systemd.io/) | user services (optional) | LGPL-2.1+ |
| [ffmpeg/libav](https://ffmpeg.org/) | optional audio conversion | LGPL/GPL (system package) |

Optional HDMI-CEC integration uses the companion `fedora-cec-toolkit`
`cec-policy` script (not bundled here). Systems without it use local media
keys.

## Fork maintenance

This fork is maintained at
<https://github.com/nasmcdermott/linux-firetv-remote>. See
[`CONTRIBUTORS.md`](../CONTRIBUTORS.md) for the people and projects credited.

## Trademarks

Amazon, Fire TV, Alexa and related marks are trademarks of Amazon.com, Inc. or
its affiliates. Apple, Apple TV and iPhone are trademarks of Apple Inc. This
project is not affiliated with or endorsed by either company.
