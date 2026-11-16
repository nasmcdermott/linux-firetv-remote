# Requirements and prerequisites

Check this list before running `./install.sh`. The installer verifies the
Python environment and some tools; `firetv-remote doctor` reports the rest
after installation.

## Baseline

| Item | Requirement | Notes |
|---|---|---|
| OS | Modern Linux | x86_64 or aarch64; glibc is the best-tested path |
| Kernel | 5.x or newer | needs `hidraw`, `evdev`, `uinput`, and Bluetooth LE |
| Python | 3.11 or newer | used for the receiver and the project `.venv`. If a required wheel is unavailable for your Python ABI, the installer uses a compatible interpreter (via `uv` when available) instead of requiring a particular distro |
| RAM | 2 GB minimum, 4 GB+ recommended | more for `medium`/`large-v3` Whisper models |
| Disk | ~2 GB plus model cache | see [Disk and models](#disk-and-models) |
| Session | Wayland or X11 | a TTY works for keys, but not typing or the PIN card |
| Network | Same LAN as the phone | UDP multicast (SSDP) must not be isolated |
| Init | systemd optional | non-systemd installs use `--no-service` |

A GPU is not required. `faster-whisper` runs on CPU with `int8` by default.

## Hardware

| Component | Required | Notes |
|---|---|---|
| BLE remote | For the physical-remote path | Amazon Fire TV Alexa Voice Remote (4K and Lite tested); other LE HID remotes may work |
| Bluetooth adapter | For the physical-remote path | must support Bluetooth LE and HID host; most built-in adapters do |
| Linux host | Yes | media PC, mini PC, laptop, or VM with USB/Bluetooth passthrough |
| Phone/tablet | For the app path | official Amazon Fire TV app; iOS works, Android needs a client-side bypass |
| HDMI-CEC adapter | Optional | lets the TV/AV receiver handle volume/mute/power when `cec-policy` owns it |

The BLE and phone paths are independent: you can install only one with
`--ble-only` or `--phone-only`.

## Required host software

| Software | Why | Notes |
|---|---|---|
| Python 3.11+ | runtime | `python3 -m venv` + pip, or `uv` (uv can provide a compatible interpreter when your system Python is newer than the voice wheels) |
| BlueZ 5.x + `bluetoothctl` | BLE pairing and discovery | not needed for `--phone-only` |
| `libopus` | Opus decode via `opuslib` | `dnf install libopus`, `apt install libopus0`, `pacman -S opus` |
| `openssl` 1.1.1+ | self-signed TLS identity | needed for `-addext` SAN support |
| `ydotool` | typing/dictation | preferred on KWin/Wayland; needs `/dev/uinput` |
| `wtype` or `xdotool` | typing fallback | used only when `ydotool` is absent |
| D-Bus tools | Menu and desktop actions | `busctl`, `gdbus`, or `dbus-send` |
| App launcher | `app:`/`desktop:` actions | one of `gtk-launch`, `gio`, `xdg-open` |
| System CA certificates | first model download | `faster-whisper` fetches from Hugging Face over TLS on first use |

The project Python packages (`faster-whisper`, `PyYAML`, `dbus-fast`,
`numpy`, `opuslib`) are installed into `.venv` by the installer. You do not
install them system-wide. The venv is checked at installer, service and CLI
startup and rebuilt automatically when a Python upgrade or interrupted install
leaves it stale.

## Optional components

| Component | Enables |
|---|---|
| `PySide6` + `layer-shell-qt` | the red top-right PIN card |
| `cec-policy` from `fedora-cec-toolkit` | HDMI-CEC volume/mute/TV power |
| `notify-send` | desktop notifications |
| `systemd` user units | start at login and restart on failure |
| `uv` | faster environment creation and a fallback interpreter when the system Python ABI is newer than the voice wheels (the installer falls back to venv+pip on 3.11-3.14) |
| `wmctrl` / `xdotool` | focus an existing window on X11 |
| `ffmpeg` | optional audio conversion for manual debugging |
| wake-word extra (`uv sync --extra wakeword`) | always-listening `07_wake_word.py`; requires Python 3.11-3.14 until `onnxruntime` ships cp315 wheels |

Without a PIN card the PIN is still printed in the journal and available from
`firetv-remote pin`. Without CEC, the installer writes local media-key
overrides for volume/mute/power.

## Permissions

The install is user-level and never needs root. Some device access does:

- **BLE remote**: the user needs read access to `/dev/hidraw*` and
  `/dev/input/event*`, usually through the `input` group. The installer prints
  the optional `udev/91-fire-remote.rules` command; log out and back in after
  adding yourself to `input`.
- **Typing**: `ydotool` needs write access to `/dev/uinput`. Most desktops grant
  this through `uaccess`; if not, add a udev rule or use `wtype`/`xdotool`.
- **Headless login**: systemd user services start at login. For a machine that
  boots without a graphical login, enable lingering for your user:
  `loginctl enable-linger "$USER"`.
- **System files**: the installer does not modify them. The udev/hwdb rules are
  optional and their commands are printed for you to run.

Check group membership:

```bash
id -nG
test -w /dev/uinput && echo "uinput writable" || echo "uinput not writable"
```

## Network

The phone and the Linux host must be on the same LAN/VLAN. Guest Wi-Fi
isolation, Docker bridges, and Waydroid NAT can block discovery.

| Port | Protocol | Purpose |
|---|---|---|
| 1900 | UDP | SSDP discovery |
| 60000 | TCP | DIAL device description |
| 8009 | TCP | DIAL app and icons |
| 8080 | TCP | Turnstile HTTPS |
| 9090 | TCP | WSS voice |

Do not expose 8080/9090 to the internet. If a firewall is active, allow those
ports on the LAN interface only. No cloud service or account is used.

The receiver needs the machine's LAN address, not just `127.0.0.1`; it binds
the detected LAN address because desktop services such as Steam may already
hold `127.0.0.1:8080`.

## Disk and models

The project `.venv` is typically a few hundred MB. Whisper models are downloaded
once and cached under `~/.cache/huggingface/`:

| Model | Approx. download | Use |
|---|---|---|
| `tiny` | ~75 MB | fastest, lowest accuracy |
| `base` | ~145 MB | fast |
| `small` | ~500 MB | default balance |
| `medium` | ~1.5 GB | more accurate, slower |
| `large-v3` | ~3 GB | best accuracy, needs more RAM |

The first transcription needs internet access to fetch the model. After that,
voice processing is fully local. To pre-cache models, run a short
`scripts/05_transcribe.py` job once before going offline.

## Phone app

- Use the **official Amazon Fire TV** app from Amazon/Apple/Google.
- **iOS/iPadOS**: works with the receiver's self-signed certificate.
- **Android**: the stock app pins Amazon's Turnstile certificate. There is no
  Linux-side fix; see [`tools/android/README.md`](../tools/android/README.md)
  for client-side options and Waydroid caveats.
- Some Android builds and Waydroid setups also block SSDP multicast. Bridged
  networking or a real device is more reliable than NAT.

## Pre-flight checklist

Run these before installing:

```bash
python3 --version                       # 3.11+
bluetoothctl show                       # adapter powered (BLE path)
id -nG                                  # contains "input" for the BLE remote
test -w /dev/uinput                     # typing/dictation
command -v openssl ydotool              # TLS + preferred typer
command -v gtk-launch gio xdg-open      # at least one app launcher
ss -ltnup | grep -E ':(1900|60000|8009|8080|9090)\b'  # ports not already taken
```

After installing:

```bash
firetv-remote doctor
firetv-remote status
journalctl --user -u firetv-receiver -f
journalctl --user -u voice-stt -f
```

## Known limitations

- Stock Android needs a client-side certificate-pinning bypass.
- Waydroid's default NAT often blocks SSDP; use bridged networking or a real
  Android device.
- Alpine and other musl distributions may lack prebuilt `ctranslate2` /
  `faster-whisper` wheels; a glibc distro is the safest choice.
- Immutable/atomic distros need a writable checkout for `.venv` and
  `config/keymap.json`.
- macOS and Windows are not supported.
- Non-systemd systems use `./install.sh --no-service` and must supervise the
  printed commands themselves.

## See also

- [INSTALL.md](INSTALL.md) — distro package names and install/uninstall
- [DESKTOP.md](DESKTOP.md) — KDE, GNOME, X11, Sway/Hyprland behavior
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — common failures and fixes
- [ARCHITECTURE.md](ARCHITECTURE.md) — services, ports, and data flow
- [UPSTREAM.md](UPSTREAM.md) — upstream projects and runtime licenses
