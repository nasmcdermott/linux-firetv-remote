# Installation

Before installing, read [`REQUIREMENTS.md`](REQUIREMENTS.md) for hardware,
permissions, ports, disk usage, and the pre-flight checklist.

## Supported systems

The project is written for Linux with Python 3.11+ and avoids hard dependencies
on one desktop or init system. The most tested setup is Fedora + KDE Plasma
Wayland, but the portable paths cover the following.

| Distro family | Packages (install what applies) |
|---|---|
| Fedora | `sudo dnf install libopus python3-uv ydotool python3-pyside6 layer-shell-qt bluez dbus-tools glib2 gtk3 openssl` |
| Debian / Ubuntu | `sudo apt install libopus0 python3-venv python3-pip ydotool python3-pyside6.qml layer-shell-qt bluez dbus libglib2.0-bin libgtk-3-bin openssl` |
| Arch | `sudo pacman -S opus uv ydotool pyside6 layer-shell-qt bluez dbus glib2 gtk3 openssl` |
| openSUSE | `sudo zypper install libopus0 python3-uv ydotool python3-pyside6 layer-shell-qt bluez dbus-1 glib2-tools gtk3 openssl` |
| Alpine | `sudo apk add opus uv ydotool py3-pyside6 bluez dbus glib gtk+3.0 openssl` |
| Other | Python 3.11+, BlueZ, libopus, a D-Bus session, `openssl`, and a Wayland/X11 session for typing |

`uv` is optional. If it is missing, the installer creates `.venv` with
`python3 -m venv` and installs `requirements.txt` with pip.

Optional:

- `cec-policy` from the companion `fedora-cec-toolkit` project for HDMI-CEC
  volume/mute/TV-on. Without it, the installer writes portable local media-key
  overrides.
- `notify-send` for desktop notifications.
- `layer-shell-qt` for a true top-right PIN card on KDE Wayland.
- `wtype` or `xdotool` as typing fallbacks when `ydotool` is unavailable.

## Install

```bash
git clone https://github.com/nasmcdermott/linux-firetv-remote.git
cd linux-firetv-remote
./install.sh
```

The installer writes only under `$HOME` and the checkout:

```text
./.venv/                                      Python environment
~/.local/bin/firetv-remote                    launcher
~/.local/bin/firetv-receiver                  compatibility symlink
~/.config/firetv-receiver/config.json         receiver config
~/.config/firetv-receiver/devices.json        paired phones
~/.config/systemd/user/*.service              user units (if systemd)
config/keymap.json                            generated portable overrides
```

The installer never runs `sudo`. If a udev/hwdb rule is missing it prints the
optional commands; set `FIRETV_INSTALL_UDEV=1` to run them automatically.
Installed services:

| Unit | Role |
|---|---|
| `remote-input.service` | BLE Menu, and volume/mute/power in portable mode |
| `remote-launch.service` | BLE app-shortcut button → `config/button_launch.yaml` |
| `voice-stt.service` | BLE + phone WSS voice → one Whisper model/command router |
| `firetv-receiver.service` | DIAL/HTTPS/PIN/app row/keys/text for the phone app |

### Options

Most installs only need `./install.sh` or `./install.sh --ble-only`.

```sh
./install.sh                    # BLE remote + official phone app
./install.sh --ble-only         # BLE buttons + voice only
./install.sh --phone-only       # official-app receiver only
./install.sh --dry-run          # preview all changes
./install.sh --uninstall        # remove units/launcher, keep pairings
```

Setup flags:

```sh
REMOTE_MAC=AA:BB:CC:DD:EE:FF ./install.sh
./install.sh --mac AA:BB:CC:DD:EE:FF  # same thing, explicit flag
./install.sh --name "Living Room"         # phone-app device name
./install.sh --no-service                 # no systemd; print commands
./install.sh --pin-display auto|qt|none
```

Advanced:

```sh
./install.sh --voice-only                 # BLE voice only
./install.sh --force-keymap               # rewrite config/keymap.json
./install.sh --force-receiver-config      # rewrite the receiver config
./install.sh --uninstall --purge          # also remove pairings/TLS/.venv
```

The older spellings `--no-receiver`, `--receiver-only` and `--receiver-name`
still work as aliases.

Set the BLE remote MAC explicitly:

```bash
REMOTE_MAC=AA:BB:CC:DD:EE:FF ./install.sh
# or
./install.sh --mac AA:BB:CC:DD:EE:FF
```

The MAC is written only to the user systemd units under
`~/.config/systemd/user`, never into the checkout. The device name shown in the
phone app is set with `--name` (default: OS hostname) and stored in
`~/.config/firetv-receiver/config.json`.

Force portable behavior for testing or unusual setups:

```bash
FIRETV_IS_KDE=0 FIRETV_CEC_OWNED=0 ./install.sh --force-keymap --dry-run
```

### Verify

```bash
firetv-remote doctor
firetv-remote devices
bash verify.sh          # KDE-only Menu/binding checks
journalctl --user -u voice-stt -f
journalctl --user -u firetv-receiver -f
```

### Update

Re-running the installer is the supported update path. It refreshes the venv,
services, launcher and generated keymap while keeping `config/keymap.json`,
`~/.config/firetv-receiver/config.json`, pairings and TLS identity.

```bash
./update.sh              # update from this checkout
./update.sh --pull       # git pull --ff-only first, then update
./update.sh --dry-run
./update.sh --force-receiver-config   # rewrite the receiver config
```

### Uninstall

```bash
./uninstall.sh                 # remove units + launcher, keep config/devices
./uninstall.sh --purge         # also remove receiver config/devices/TLS and .venv
./uninstall.sh --voice-only    # only the voice service
```

## Non-systemd systems

`./install.sh --no-service` installs files, config and the launcher, then prints
the commands to run in separate terminals:

```bash
# start voice first: it owns WSS 9090 and auto-detects the Amazon remote
/path/to/linux-firetv-remote/.venv/bin/python \
  scripts/06_voice_stt.py --type --wss

/usr/bin/python3 scripts/remote_input.py --portable
/path/to/linux-firetv-remote/.local/bin/firetv-remote run -v
```

Add `--mac AA:BB:CC:DD:EE:FF` only to select one of several connected
Amazon remotes. Without it both daemons discover the remote by HID vendor.

Use `--portable` only when nothing else owns volume/mute/power (no
`cec-remote-observer`); otherwise it would send those actions twice.

Use your init system (OpenRC, runit, s6, etc.) to supervise those commands.

### Optional wake word

The base install does not include the always-listening wake word. Install it
with `uv sync --extra wakeword` (or `pip install -r requirements-wakeword.txt`)
and run `scripts/07_wake_word.py`. It currently needs Python 3.11-3.14 because
its `onnxruntime` dependency has no cp315 wheels yet; the Fire TV remote and
phone-app voice paths do not use it.

## udev and uinput

- `udev/91-fire-remote.rules` grants the `input` group access to the Amazon
  remote's hidraw node. The installer prints the `sudo cp` command; log out/in
  after installing if your user was just added to `input`.
- `udev/90-fire-remote-voice.hwdb` maps Select to Enter, Back to Escape, and
  reserves the voice scancode that would otherwise open KRunner. It does not
  affect the raw hidraw voice stream. The installer prints the command.
- `ydotool` needs write access to `/dev/uinput`; most desktops grant it through
  `uaccess`. If not, add a udev rule or use `wtype`/`xdotool`.

## Firewall

The phone app needs, on the LAN only:

```text
UDP 1900        SSDP discovery
TCP 60000       DIAL device description
TCP 8009        DIAL app + icons
TCP 8080        Turnstile HTTPS
TCP 9090        WSS voice
```

Do not expose 8080/9090 to the internet. The protocol is a LAN remote-control
service with a shared API key and per-phone tokens.
