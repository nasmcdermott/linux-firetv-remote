# Desktop compatibility

The project is designed to work on KDE Plasma, GNOME, and other Wayland/X11
desktops. The installer detects KDE and CEC ownership and writes portable
overrides only when needed.

## What the installer chooses

| Condition | Keymap overrides |
|---|---|
| KDE + CEC | none; built-in defaults |
| KDE, no CEC | volume/mute/power → local media keys |
| non-KDE + CEC | Menu → `KEY_MENU`, Home → `Super` |
| non-KDE, no CEC | both of the above |

The generated file is `config/keymap.json`; it is local and gitignored. Delete
it to return to built-ins, or use `./install.sh --force-keymap` to regenerate.

## KDE Plasma

- **Menu**: Plasma `activateLauncherMenu` D-Bus (Bigscreen overlay).
- **Home**: `KEY_HOMEPAGE` (Bigscreen home screen).
- **PIN card**: layer-shell top-right when `layer-shell-qt` is available.
- **Volume/mute/power**: HDMI-CEC via `cec-policy` when
  `cec-remote-observer.service` owns them.
- **App launch**: `kstart5` first, so running windows are activated.
- **Verify**: `bash verify.sh`.

On Plasma Bigscreen, the physical remote's Select/Back are normalized by the
optional hwdb file so they work in fullscreen UIs.

## GNOME

- **Menu**: `KEY_MENU` (portable override).
- **Home**: `Super` (overview).
- **PIN card**: transparent fullscreen overlay with the card top-right.
- **App launch**: `gtk-launch`/`gio launch`/`xdg-open`.
- **Typing**: `ydotool` first; `wtype` fallback.
- **Volume**: local media keys unless CEC is explicitly configured.

## XFCE, Cinnamon, MATE, X11

- **PIN card**: normal top-right window.
- **App launch**: `gtk-launch`/`gio launch`/`xdg-open`; `wmctrl`/`xdotool` for
  focusing existing windows.
- **Typing**: `ydotool` (uinput) or `xdotool`.
- **Menu/Home**: portable key overrides (`KEY_MENU`, `Super`).
- **Volume**: desktop media keys.

## Sway, Hyprland, wlroots

- **PIN card**: layer-shell when the compositor provides
  `org.kde.layershell`; otherwise a normal window or terminal PIN.
- **Typing**: `ydotool` works through uinput; `wtype` is a fallback.
- **Menu/Home**: portable key overrides.
- **App launch**: `gtk-launch`/`gio launch`/`xdg-open`.

## TTY / kiosk

The receiver and BLE daemons do not need a graphical session for keys/text.
`ydotool`/uinput still injects into the active VT. The Qt PIN card needs a
graphical session; otherwise the PIN is printed in the journal and
`firetv-remote pin`.

## Customizing

Edit `config/keymap.json` (receiver + BLE Menu) and
`config/voice_assistant.yaml` (voice/commands). For per-user overrides of voice
settings, create `config/voice_assistant.local.yaml`.

Useful action types:

```json
{
  "keys": {
    "menu": "key:menu",
    "home": "key:leftmeta",
    "volume_up": "key:volumeup",
    "power": "key:power"
  }
}
```

`app:` actions use the Fire Remote Client launcher; `desktop:` actions use
`gtk-launch`/`gio`/`xdg-open`; `command:` actions run a configured command.
