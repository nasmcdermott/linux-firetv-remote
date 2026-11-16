# Notices and attribution

This project combines the **BLE Fire TV remote** pipeline with a receiver for
the **official Amazon Fire TV app** (DIAL/HTTPS/WSS). It is an independent
Linux implementation, not affiliated with or endorsed by Amazon.

The BLE half is based on the MIT-licensed
[`justanotheruseer/fire-remote-client`](https://github.com/justanotheruseer/fire-remote-client)
work; see [`LICENSE`](LICENSE). The PIN card is adapted from
[`linux-apple-remote`](https://github.com/nasmcdermott/linux-apple-remote).

## Pairing PIN card

The Qt Quick pairing card, its palette reader, and the layer-shell/window
launcher are adapted from the custom PIN-card implementation in
**linux-apple-remote**:

- <https://github.com/nasmcdermott/linux-apple-remote>
- MIT License, Copyright (c) 2026 linux-apple-remote contributors
- Full license: [`LICENSES/linux-apple-remote-MIT.txt`](LICENSES/linux-apple-remote-MIT.txt)

The PIN card is original to `linux-apple-remote`; it is **not** derived from
**windows-apple-remote**. `windows-apple-remote` contributed the Apple TV
Companion server in `linux-apple-remote`, which this project does not use.
`linux-apple-remote`'s LICENSE nevertheless carries a derivation notice for
`windows-apple-remote` by Arya Kavian (MIT), so that MIT text is retained in
[`LICENSES/windows-apple-remote-MIT.txt`](LICENSES/windows-apple-remote-MIT.txt)
for completeness. No `windows-apple-remote` code is present in this repository.

The visible strings and accent color were changed for Fire TV (red instead of
the desktop accent, “Fire TV Remote” instead of “Apple TV Remote”). No Apple TV
code or assets are included.

## Qt / KDE components

The PIN card uses PySide6, Qt Quick, KDE layer-shell-qt, and the KDE/KWin color
interfaces. Those components keep their own licenses.

## Protocol research

Discovery, Turnstile HTTPS, certificate pinning, app list, and WSS voice facts
are documented in [`docs/UPSTREAM.md`](docs/UPSTREAM.md). No Amazon code,
certificate, private key, or firmware is included.

## Trademarks

Amazon, Fire TV, Alexa, and related marks are trademarks of Amazon.com, Inc. or
its affiliates. Apple, Apple TV, and iPhone are trademarks of Apple Inc. This
project is not affiliated with or endorsed by either company.
