# Official Fire TV app receiver

The receiver implements the LAN protocol used by the official Amazon Fire TV
app. It is not a third-party remote clone.

## Protocol surface

| Step | Endpoint |
|---|---|
| Discovery | SSDP `urn:dial-multiscreen-org:service:dial:1`, `dd.xml`, `Application-URL` |
| DIAL app | `GET/POST http://<ip>:8009/apps/FireTVRemote` |
| Status | `GET /v1/FireTV/status` |
| Device info | `GET /v1/FireTV` |
| Properties | `GET/POST /v1/FireTV/properties` |
| Keys | `POST /v1/FireTV?action=...` |
| Media | `POST /v1/media?action=play|scan` |
| Text | `POST /v1/FireTV/text`, `POST /v1/FireTV/keyboard` |
| Apps | `GET /v1/FireTV/apps`, `POST /v1/FireTV/app/<id>` |
| Voice | `POST /v1/FireTV/voiceCommand?action=start|stop`, WSS 9090 |
| Pairing | `POST /v1/FireTV/pin/display`, `pin/verify` |

The app uses `x-api-key: 0987654321` (public) and a per-phone
`x-client-token` from PIN pairing. Return an empty `turnstileVersion` to select
the smaller v1 endpoint family.

## PIN card

The card runs in the system Python with PySide6:

- red `#e01b24` accent, “Fire TV Remote”, “Enter this PIN in the Fire TV app”;
- KDE layer-shell top-right, GNOME fullscreen overlay, X11/other window;
- `--pin-display auto|qt|none`;
- if Qt is unavailable, the PIN is printed in the journal and
  `firetv-remote pin`.

The PIN expires after 60 seconds, with five wrong-attempt throttling. Paired
phones are remembered in `~/.config/firetv-receiver/devices.json`.

## App row and icons

`config/keymap.json` (copy `config/keymap.example.json`):

```json
{
  "apps": {
    "linux.firefox": {"action": "app:Firefox", "name": "Firefox"},
    "my.kodi": {"action": "app:Kodi", "name": "Kodi"},
    "my.custom": {
      "action": "desktop:my-app.desktop",
      "name": "My App",
      "icon": "/path/to/icon.png"
    }
  }
}
```

- `app:<name>` uses the shared `CommandHandler.launch_app` (kstart5 window
  activation, redirects, fuzzy names).
- `desktop:<id>` uses `gtk-launch`/`gio`/`xdg-open`.
- `icon` can be PNG/JPEG/SVG, an http(s) URL, or omitted for a theme icon.
- `"advertise": false` hides an entry but keeps it launchable.
- Icons are served from `http://<ip>:8009/apps/icon/<id>.png`; the app's
  `._SX210_` suffix is accepted.

## CLI

```bash
firetv-remote status
firetv-remote doctor
firetv-remote pair
firetv-remote pin
firetv-remote devices
firetv-remote forget "iPhone"
firetv-remote keys
firetv-remote preview-pin --ttl 5
firetv-remote run -v
```

The legacy `firetv-receiver` command is a compatibility symlink.

## Android

The stock Android app pins Amazon's embedded Turnstile certificate. There is no
server-side fix. See `tools/android/README.md`. The official iOS app accepts the
receiver's self-signed certificate.
