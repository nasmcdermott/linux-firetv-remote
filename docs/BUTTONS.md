# BLE buttons and remapping

## Button map

The Fire TV Stick 4K remote reports three input reports over HID-over-GATT.
The daemons read the remote through `/dev/hidrawN`; the kernel also presents
the keyboard/consumer keys as evdev.

| Report | Button → code |
|---|---|
| `0x01` keyboard | D-pad up/down/left/right, select, back |
| `0x02` consumer | voice, home, menu, mute, vol±, play/pause, rewind, fast-forward, TV |
| `0xEF` vendor | app-shortcut keys (Prime/Netflix/Disney/Peacock on some SKUs) |

The voice button is consumer usage `0x0221`; its raw HID audio report (`0xF0`)
is separate from the kernel key event.

## Ownership

| Button | Owner |
|---|---|
| Menu | `remote-input.service` → shared `receiver/actions.py` keymap |
| Volume/mute/power | `cec-remote-observer.service` when CEC is present; otherwise `remote-input --portable` |
| Voice | `voice-stt.service` (also serves phone WSS) |
| App-shortcut | `remote-launch.service` → `config/button_launch.yaml` |
| D-pad/select/back | kernel evdev → desktop/Plasma |

Do not run `cec-remote-observer.service` and `remote-input --portable` at the
same time for volume/power; that would send every action twice. The installer
chooses one based on whether `cec-remote-observer.service` is active.

## Portable volume/power

On systems without `cec-remote-observer`:

```bash
python3 scripts/remote_input.py --mac AA:BB:CC:DD:EE:FF --portable
```

This overrides the loaded keymap’s volume/mute/power entries with
`KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, `KEY_MUTE`, `KEY_POWER` and dispatches them
through the shared action runner. The installer adds `--portable` and writes
local keymap overrides for the receiver as well.

## App-shortcut button

`config/button_launch.yaml` (created by the installer from
`config/button_launch.example.yaml`; the live file is gitignored):

```yaml
device: auto
grab: false
cooldown: 0.4
buttons:
  - name: my-app
    key: KEY_PROGRAM
    scan: 0xc008d
    action: ~/.local/share/applications/my-app.desktop
```

### Interactive remap

The easiest way to create a mapping is `firetv-remote remap`:

```bash
firetv-remote remap                              # capture a button, then enter an action
firetv-remote remap --action "systemctl --user start my-app.service"
firetv-remote remap --desktop 3                  # pick from --list-desktops
firetv-remote remap --button KEY_PROGRAM --scan 0xc008d --action "my-script --flag"
firetv-remote remap --list
firetv-remote remap --remove my-script
firetv-remote remap --reset
```

It listens on the remote’s evdev keyboard interface, captures the evdev key and
MSC_SCAN value, writes `config/button_launch.yaml`, and restarts
`remote-launch.service`. `--timeout`, `--device`, `--file` and `--no-restart`
are available for unusual setups.

Vendor app-shortcut buttons can all report `KEY_PROGRAM`; the captured
`scan` is what distinguishes Prime/Netflix/Disney/Peacock. Use `--list-keys`
for known key names and `--list-desktops` for app numbers.

The action can be a `.desktop` file, any file/directory (`xdg-open`), or a raw
shell command. Executable files (AppImages, scripts, binaries) are run directly
instead of through `xdg-open`/KIO. `scripts/remap_button.py` remains as a legacy
app-only picker.

## Menu

On KDE the Menu action calls `org.kde.PlasmaShell.activateLauncherMenu` over
D-Bus (with `busctl`/`gdbus`/`dbus-send` fallbacks). On non-KDE desktops the
installer overrides it to `key:menu` in `config/keymap.json`. Customize the
`menu` entry to change both the BLE and phone behavior.

`remote-input.service` finds the remote by its Amazon HID vendor (`0x0171`),
so `REMOTE_MAC` is optional. A re-pair or distro upgrade that changes the
bluetooth address does not break Menu. Pass `REMOTE_MAC=... ./install.sh` only
when several Amazon remotes are connected and you need a specific one.

## Home

The phone app’s Home action defaults to `KEY_HOMEPAGE` on KDE (Bigscreen home)
and `Super` on other desktops via the portable keymap. The BLE Home key reaches
the desktop through the kernel; adjust the desktop shortcut if needed.
