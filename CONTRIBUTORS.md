# Contributors

## Implementation

- **nasmcdermott** — <https://github.com/nasmcdermott>
  Fork maintenance, official-app receiver, GUI PIN card, shared voice pipeline,
  safe-command gate, portability work, tests, and release preparation.

## Upstream and design credit

- **justanotheruseer** — original [`fire-remote-client`](https://github.com/justanotheruseer/fire-remote-client)
  BLE capture, Opus voice pipeline, command handler, and service/install
  patterns. Retained under the MIT license in [LICENSE](LICENSE).
- **linux-apple-remote contributors** — custom PIN-card implementation,
  palette and layer-shell/window launcher. MIT; see
  [LICENSES/linux-apple-remote-MIT.txt](LICENSES/linux-apple-remote-MIT.txt)
  and [NOTICE.md](NOTICE.md).

## Protocol research for the official-app receiver

The DIAL/HTTPS/WSS receiver was implemented from protocol facts documented by
these public projects. None of their code or assets are copied into this
repository; see [`docs/UPSTREAM.md`](docs/UPSTREAM.md) for the full table.

- [`Edx0n/firetv-remote-control-adfree`](https://github.com/Edx0n/firetv-remote-control-adfree)
  — static analysis of the official Android app: DIAL, Turnstile endpoints, PIN
  flow, app list/icons, keyboard and voice.
- [`LibreControl/fire-tv`](https://github.com/LibreControl/fire-tv) — discovery,
  pairing, keys/media/text/apps/voice reference implementation.
- [`pyfiretvremote`](https://pypi.org/project/pyfiretvremote/) — async client
  reference for pairing and voice.
- [`hms-homelab/hms-firetv`](https://github.com/hms-homelab/hms-firetv) —
  Turnstile endpoints and WSS 9090 voice notes.
- Home Assistant community Fire TV REST-command discussions — independent
  confirmation of the public API key, endpoints and headers.
- A public Smart TV Emulator project — existence proof that the stock iOS app
  can drive a software Fire TV.
- [`Staars/berry-examples`](https://github.com/Staars/berry-examples) — BLE
  voice enable/HID/Opus reference for the physical remote.

Protocol research credits, runtime dependencies and their licenses are listed
in [`docs/UPSTREAM.md`](docs/UPSTREAM.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributors keep their own copyright
for new work; by submitting a change you agree it may be distributed under the
project's MIT license.
