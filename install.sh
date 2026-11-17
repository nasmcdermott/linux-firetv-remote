#!/usr/bin/env bash
# Install the Fire TV remote for Linux at user level.
#
# Common:
#   ./install.sh                    BLE remote + official phone app
#   ./install.sh --ble-only         BLE remote only (buttons + voice)
#   ./install.sh --phone-only       official phone app only
#   ./install.sh --dry-run          preview every change
#   ./install.sh --uninstall        remove units/launcher (keep pairings)
#   ./install.sh --help
#
# Setup:
#   REMOTE_MAC=AA:BB:CC:DD:EE:FF ./install.sh
#   ./install.sh --mac AA:BB:CC:DD:EE:FF
#   ./install.sh --name "Living Room"
#   ./install.sh --no-service       no systemd; print foreground commands
#   ./install.sh --pin-display auto|qt|none
#
# Advanced:
#   ./install.sh --voice-only              BLE voice only
#   ./install.sh --force-keymap            regenerate config/keymap.json
#   ./install.sh --force-receiver-config   regenerate receiver config
#   ./install.sh --uninstall --purge       also remove pairings and TLS identity
#
# Everything installs under $HOME and this checkout. The installer never
# uses sudo; optional udev/hwdb commands are printed for you. See docs/INSTALL.md.
set -euo pipefail

usage() {
  sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_MAC="${REMOTE_MAC:-}"
KG="$HOME/.config/kglobalshortcutsrc"
KG_BAK="$HOME/.config/kglobalshortcutsrc.frc-bak"
UDEV_RULE_SRC="$REPO_ROOT/udev/91-fire-remote.rules"
UDEV_RULE_DEST="/etc/udev/rules.d/91-fire-remote.rules"
UDEV_HWDB_SRC="$REPO_ROOT/udev/90-fire-remote-voice.hwdb"
UDEV_HWDB_DEST="/etc/udev/hwdb.d/90-fire-remote-voice.hwdb"

VOICE_UNIT="voice-stt.service"
UNIT="remote-input.service"
LAUNCH_UNIT="remote-launch.service"
RECEIVER_UNIT="firetv-receiver.service"
RECEIVER_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/firetv-receiver"
RECEIVER_CONFIG="$RECEIVER_CONFIG_DIR/config.json"
RECEIVER_BIN="$HOME/.local/bin/firetv-remote"
RECEIVER_COMPAT="$HOME/.local/bin/firetv-receiver"
KG_GROUP="plasmashell"
KG_ACTION="activate widget 87"
KG_FRIENDLY="Activate Plasma Bigscreen Homescreen Widget"
# KDE writes multiple shortcuts per action as a LITERAL two-character "\t"
# sequence, NOT a real tab byte. A real 0x09 makes kglobalaccel silently ignore
# the whole entry, so single quotes keep backslash-t literal here.
KG_DEFAULT='Meta+F1\tMenu'
KG_ALT="Meta+F1"
KG_TASKS_ACTION="Toggle Bigscreen Tasks Overview"

DRY=0
UNINSTALL=0
VOICE_ONLY=0
RECEIVER=1
RECEIVER_ONLY=0
RECEIVER_NAME=""
PIN_DISPLAY="auto"
FORCE_RECEIVER_CONFIG=0
FORCE_KEYMAP=0
INSTALL_SERVICE=1
PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY=1; shift ;;
    --uninstall)   UNINSTALL=1; shift ;;
    --ble-only|--no-receiver) RECEIVER=0; shift ;;
    --phone-only|--receiver-only) RECEIVER=1; RECEIVER_ONLY=1; shift ;;
    --receiver)    RECEIVER=1; shift ;;
    --voice-only)  VOICE_ONLY=1; RECEIVER=0; shift ;;
    --name|--receiver-name) RECEIVER_NAME="${2:?missing value}"; shift 2 ;;
    --mac|--remote-mac) REMOTE_MAC="${2:?missing value}"; shift 2 ;;
    --pin-display) PIN_DISPLAY="${2:?missing value}"; shift 2 ;;
    --force-receiver-config) FORCE_RECEIVER_CONFIG=1; shift ;;
    --force-keymap) FORCE_KEYMAP=1; shift ;;
    --no-service)  INSTALL_SERVICE=0; shift ;;
    --purge)       PURGE=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

case "$PIN_DISPLAY" in
  auto|qt|none) ;;
  *) echo "error: --pin-display must be auto, qt or none" >&2; exit 2 ;;
esac

# Desktop/distro detection: these choose portable defaults instead of assuming
# KDE + fedora-cec-toolkit.
DESKTOP_ENV="$(printf '%s:%s:%s' \
  "${XDG_CURRENT_DESKTOP:-}" "${DESKTOP_SESSION:-}" "${KDE_FULL_SESSION:-}" \
  | tr '[:upper:]' '[:lower:]')"
IS_KDE=0
case "$DESKTOP_ENV" in *kde*|*plasma*) IS_KDE=1 ;; esac
if [[ -n "${FIRETV_IS_KDE:-}" ]]; then
  case "${FIRETV_IS_KDE}" in 1|true|yes) IS_KDE=1 ;; *) IS_KDE=0 ;; esac
fi
HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1; then HAVE_SYSTEMD=1; fi
CEC_OWNED=0
if (( HAVE_SYSTEMD )) \
  && systemctl --user is-active --quiet cec-remote-observer.service 2>/dev/null; then
  CEC_OWNED=1
fi
if [[ -n "${FIRETV_CEC_OWNED:-}" ]]; then
  case "${FIRETV_CEC_OWNED}" in 1|true|yes) CEC_OWNED=1 ;; *) CEC_OWNED=0 ;; esac
fi
if (( INSTALL_SERVICE )) && (( ! HAVE_SYSTEMD )); then
  INSTALL_SERVICE=0
  echo "note: systemd not found; installing files/config only. Run the foreground commands printed at the end." >&2
fi

run() { if (( DRY )); then printf '   [dry-run] %s\n' "$*"; else eval "$*" || true; fi; }
is_mac() { [[ "$1" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; }
if [[ -n "$REMOTE_MAC" ]] && ! is_mac "$REMOTE_MAC"; then
  echo "error: remote MAC must look like AA:BB:CC:DD:EE:FF (got: $REMOTE_MAC)" >&2
  exit 2
fi
mac_arg() { if [[ -n "$REMOTE_MAC" ]]; then printf ' --mac %s' "$REMOTE_MAC"; fi; }

# Current connected Amazon remote (Bluetooth HID vendor 0x0171).
detect_remote_mac() {
  local u mac
  for u in /sys/class/hidraw/hidraw*/device/uevent; do
    [[ -f "$u" ]] || continue
    grep -qiE '^HID_ID=0005:00000171:' "$u" || continue
    mac="$(awk -F= 'tolower($1)=="hid_uniq"{print tolower($2); exit}' "$u")"
    if is_mac "$mac"; then printf '%s\n' "$mac"; return 0; fi
  done
  return 1
}

# MAC from an already installed unit, so reinstalling without REMOTE_MAC
# cannot replace a real address with the placeholder.
existing_remote_mac() {
  local unit mac
  for unit in "$HOME/.config/systemd/user/$UNIT" "$HOME/.config/systemd/user/$VOICE_UNIT"; do
    [[ -f "$unit" ]] || continue
    mac="$(sed -n 's/^Environment=REMOTE_MAC=//p' "$unit" | head -n1 | tr 'A-F' 'a-f')"
    if is_mac "$mac" && [[ "$mac" != "aa:bb:cc:dd:ee:ff" ]]; then
      printf '%s\n' "$mac"
      return 0
    fi
  done
  return 1
}

# Live Plasma shortcut change for a plasmashell action. Keys is a raw JSON list
# ('[]' clears, '[16777301]' = Qt::Key_Menu). Bypasses run(): eval would strip
# the double quotes around the action id and gdbus would reject the call.
kg_foreign() {
  local action="$1" keys="$2"
  if (( DRY )); then printf '   [dry-run] setForeignShortcut %s %s\n' "$action" "$keys"; return 0; fi
  gdbus call --session --dest=org.kde.kglobalaccel --object-path=/kglobalaccel \
    --method=org.kde.KGlobalAccel.setForeignShortcut \
    "[\"plasmashell\",\"$action\",\"\",\"\"]" "$keys" >/dev/null 2>&1 || true
}
say() { printf '== %s\n' "$*"; }

# Install a systemd user unit, stamping in this checkout's path and the MAC.
install_unit() {
  local unit="$1"
  local src="$REPO_ROOT/systemd/$unit"
  local dest="$HOME/.config/systemd/user/$unit"
  [[ -f "$src" ]] || { echo "missing $src" >&2; return 1; }
  if (( DRY )); then
    printf '   [dry-run] install %s -> %s\n' "$src" "$dest"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  install -m 0644 "$src" "$dest"
  if [[ -n "$REMOTE_MAC" ]]; then
    sed -i "s|^Environment=REMOTE_MAC=.*|Environment=REMOTE_MAC=$REMOTE_MAC|" "$dest"
  fi
  # systemd does not expand $VARS in ExecStart itself, only in its arguments,
  # so the checkout path has to be literal in the installed unit.
  sed -i "s|^Environment=FRP=.*|Environment=FRP=$REPO_ROOT|" "$dest"
  sed -i "s|\${FRP}|$REPO_ROOT|g" "$dest"
  # Keep the service on the same config dir as the CLI/installer.
  sed -i "s|^Environment=XDG_CONFIG_HOME=.*|Environment=XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}|" "$dest"
  # Portable mode: no cec-remote-observer means remote_input handles vol/mute/power.
  local ri_flags=""
  if [[ "$CEC_OWNED" -eq 0 ]]; then ri_flags="--portable"; fi
  sed -i "s|\${REMOTE_INPUT_FLAGS}|$ri_flags|" "$dest"
}

# Rewrite one kglobalshortcutsrc key in place. configparser is avoided on
# purpose: it rewrites the whole file and can mangle KDE's format, so we splice
# the single line instead.
kg_set() {
  local key="$1" value="$2" file="$3"
  python3 - "$file" "$KG_GROUP" "$key" "$value" <<'PY'
import sys

path, group, key, value = sys.argv[1:5]
prefix = key + "="
new = prefix + value

lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
in_group = done = False
out = []
for line in lines:
    if line.startswith("[") and line.rstrip().endswith("]"):
        in_group = line.strip() == "[" + group + "]"
        out.append(line)
        continue
    if in_group and line.startswith(prefix):
        out.append(new + "\n")
        done = True
        continue
    out.append(line)

if not done:
    if in_group:
        out.append(new + "\n")
    else:
        out.append("[" + group + "]\n" + new + "\n")

open(path, "w", encoding="utf-8").writelines(out)
print("  " + prefix + value)
PY
}

write_portable_keymap() {
  local path="$REPO_ROOT/config/keymap.json"
  if [[ "$CEC_OWNED" -eq 1 && "$IS_KDE" -eq 1 && ! -f "$path" ]]; then
    say "CEC + KDE detected; built-in keymap is already portable"
    return
  fi
  if [[ -f "$path" && "$FORCE_KEYMAP" -eq 0 ]]; then
    say "keeping existing $path (use --force-keymap to rewrite managed keys)"
    return
  fi
  say "write portable keymap overrides: $path"
  if (( DRY )); then
    printf '   [dry-run] CEC_OWNED=%s IS_KDE=%s (preserving apps/media)\n' \
      "$CEC_OWNED" "$IS_KDE"
    return
  fi
  python3 - "$path" "$CEC_OWNED" "$IS_KDE" <<'PY'
import json
import os
import sys

path, cec_s, kde_s = sys.argv[1:4]
cec = cec_s == "1"
kde = kde_s == "1"

data = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        data = {}

keys = dict(data.get("keys") or {})
local_media = ("volume_up", "volume_down", "mute", "power")
desktop_keys = ("menu", "home")

if cec:
    for name in local_media:
        keys.pop(name, None)
else:
    keys.update({
        "volume_up": "key:volumeup",
        "volume_down": "key:volumedown",
        "mute": "key:mute",
        "power": "key:power",
    })

if kde:
    for name in desktop_keys:
        keys.pop(name, None)
else:
    # No Plasma: use plain keys instead of Plasma Bigscreen D-Bus/KEY_HOMEPAGE.
    keys.update({
        "menu": "key:menu",
        "home": "key:leftmeta",
    })

if keys:
    data["keys"] = keys
else:
    data.pop("keys", None)

if not data or set(data) == {"_comment"}:
    # Nothing managed and no user sections: remove the generated file.
    try:
        os.unlink(path)
    except OSError:
        pass
    print("  removed empty generated keymap")
else:
    data.setdefault(
        "_comment",
        "Local overrides generated by install.sh. Edit freely; "
        "delete this file to restore the built-in defaults.",
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
PY
}

install_deps() {
  say "check runtime dependencies"
  local missing=()
  command -v python3 >/dev/null || missing+=(python3)
  command -v ydotool >/dev/null || missing+=(ydotool)

  if (( DRY )); then
    say "ensure project venv (rebuilt automatically if the system Python changed)"
  elif ! python3 "$REPO_ROOT/scripts/ensure_venv.py"; then
    echo "error: could not prepare the project venv; see the messages above." >&2
    exit 1
  fi

  if (( ${#missing[@]} )); then
    printf '   missing: %s\n' "${missing[*]}" >&2
    cat >&2 <<'EOF'
   Distro packages (install what applies):
     Fedora:        sudo dnf install libopus python3-uv ydotool python3-pyside6 layer-shell-qt
     Debian/Ubuntu: sudo apt install libopus0 python3-venv python3-pip ydotool python3-pyside6.qml
     Arch:          sudo pacman -S opus uv ydotool pyside6 layer-shell-qt
     openSUSE:      sudo zypper install libopus0 python3-uv ydotool python3-pyside6 layer-shell-qt
     Alpine:        sudo apk add opus uv ydotool py3-pyside6
   Then rerun ./install.sh (uv is optional; the installer falls back to venv+pip).
EOF
  else
    echo "   all present"
  fi

  # udev rule so a non-root daemon can open the remote's hidraw node.
  # Never write outside $HOME automatically; print the optional commands.
  if (( ! RECEIVER_ONLY )) && [[ -f "$UDEV_RULE_DEST" ]]; then
    echo "   udev rule already installed"
  elif (( ! RECEIVER_ONLY )); then
    say "optional: allow hidraw access to the remote (needs sudo, run yourself)"
    printf '   sudo cp %s %s\n' "$UDEV_RULE_SRC" "$UDEV_RULE_DEST"
    printf '   sudo udevadm control --reload-rules && sudo udevadm trigger\n'
    if [[ "${FIRETV_INSTALL_UDEV:-0}" == 1 ]] && command -v udevadm >/dev/null 2>&1; then
      run "sudo cp '$UDEV_RULE_SRC' '$UDEV_RULE_DEST'"
      run "sudo udevadm control --reload-rules && sudo udevadm trigger"
    fi
  fi

  # Optional input-layer fixes: Select=Enter, Back=Esc, voice scancode unused.
  if (( ! RECEIVER_ONLY )) && [[ -f "$UDEV_HWDB_DEST" ]]; then
    echo "   udev hwdb fixes already installed"
  elif (( ! RECEIVER_ONLY )); then
    say "optional: normalize Select/Back and free the voice scancode"
    printf '   sudo cp %s %s\n' "$UDEV_HWDB_SRC" "$UDEV_HWDB_DEST"
    printf '   sudo systemd-hwdb update\n'
    if [[ "${FIRETV_INSTALL_UDEV:-0}" == 1 ]] && command -v systemd-hwdb >/dev/null 2>&1; then
      run "sudo cp '$UDEV_HWDB_SRC' '$UDEV_HWDB_DEST'"
      run "sudo systemd-hwdb update"
    fi
  fi

  # ydotoold injects keystrokes on Wayland; dictation types through it.
  if command -v systemctl >/dev/null 2>&1 \
    && ! systemctl --user list-unit-files 2>/dev/null | grep -q '^ydotoold'; then
    say "note: ydotoold user service missing — dictation needs it"
    printf '   cp systemd/ydotoold.service ~/.config/systemd/user/\n'
    printf '   systemctl --user daemon-reload && systemctl --user enable --now ydotoold\n'
  elif ! command -v systemctl >/dev/null 2>&1; then
    echo "   note: run ydotoold in the background if your distro has no systemd"
  fi

  if (( RECEIVER )) && [[ "$PIN_DISPLAY" != "none" ]]; then
    if ! python3 -c 'import PySide6' >/dev/null 2>&1; then
      say "note: PySide6 missing — the red PIN card will not appear"
      cat >&2 <<'EOF'
   Fedora:        sudo dnf install python3-pyside6 layer-shell-qt
   Debian/Ubuntu: sudo apt install python3-pyside6.qml
   Arch:          sudo pacman -S pyside6 layer-shell-qt
   openSUSE:      sudo zypper install python3-pyside6 layer-shell-qt
   Alpine:        sudo apk add py3-pyside6
EOF
    fi
  fi

  # The voice-stt service now also serves WSS 9090, so it needs the receiver's
  # TLS identity before either service starts. Generate it once here.
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    say "ensure receiver TLS identity"
    if (( DRY )); then
      echo "   [dry-run] generate ~/.config/firetv-receiver/poc-tls/cert.pem"
    else
      (cd "$REPO_ROOT" && .venv/bin/python - <<'PY'
from pathlib import Path
from receiver import dial, server
cert_dir = Path.home() / '.config' / 'firetv-receiver' / 'poc-tls'
server.ensure_certificate(cert_dir, dial.detect_lan_ip(), dial.default_device_name())
PY
      )
    fi
  fi
}

# Wait briefly for voice-stt to bind WSS 9090 before the receiver starts, so
# the receiver delegates phone voice instead of racing it with a second model.
wait_for_voice_port() {
  (( DRY )) && return 0
  command -v ss >/dev/null 2>&1 || return 0
  local i
  for i in {1..40}; do
    ss -ltnH 'sport = :9090' 2>/dev/null | grep -q . && return 0
    sleep 0.25
  done
  echo "note: WSS 9090 is not listening yet; the receiver may use its own model" >&2
}

install_voice() {
  say "install $VOICE_UNIT"
  if (( ! INSTALL_SERVICE )); then
    say "no systemd: run the voice daemon manually:"
    printf '   %s %s/scripts/06_voice_stt.py%s --type --wss\n' \
      "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT" "$(mac_arg)"
    return
  fi
  install_unit "$VOICE_UNIT"
  if (( HAVE_SYSTEMD )); then run "systemctl --user daemon-reload"; fi
  if systemctl --user is-active --quiet "$RECEIVER_UNIT" 2>/dev/null; then
    say "stop $RECEIVER_UNIT so voice-stt can own WSS 9090"
    run "systemctl --user stop $RECEIVER_UNIT"
  fi
  say "enable + start $VOICE_UNIT"
  if systemctl --user is-active --quiet "$VOICE_UNIT" 2>/dev/null; then
    run "systemctl --user restart $VOICE_UNIT"
  else
    run "systemctl --user enable --now $VOICE_UNIT"
  fi
  wait_for_voice_port
}

install_buttons() {
  say "install $UNIT"
  if (( INSTALL_SERVICE )); then
    install_unit "$UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user daemon-reload"; fi
  fi

  if (( IS_KDE )); then
    say "Menu -> $UNIT over D-Bus; removing Plasma's native Menu bindings"
    if [[ ! -f "$KG_BAK" && -f "$KG" ]]; then
      run "cp '$KG' '$KG_BAK'"
      say "  backed up kglobalshortcutsrc -> $KG_BAK"
    fi
    run "mkdir -p \"\$HOME/.config\" && touch '$KG'"
    # Live first (kglobalaccel rewrites the file from memory), then the file for
    # the next login. Keeps Meta+F1 as the overlay's keyboard shortcut.
    kg_foreign "$KG_ACTION" '[]'
    run "kg_set '$KG_ACTION' 'none,$KG_ALT,$KG_FRIENDLY' '$KG'"
    run "kg_set '$KG_TASKS_ACTION' 'none,none,$KG_TASKS_ACTION' '$KG'"
  else
    say "non-KDE desktop: Menu/Home use the portable config/keymap.json overrides"
  fi

  if (( INSTALL_SERVICE )); then
    say "enable + start $UNIT"
    if systemctl --user is-active --quiet "$UNIT" 2>/dev/null; then
      run "systemctl --user restart $UNIT"
    else
      run "systemctl --user enable --now $UNIT"
    fi
  else
    say "no systemd: run the BLE button daemon manually:"
    local ri_flags=""
    if [[ "$CEC_OWNED" -eq 0 ]]; then ri_flags="--portable"; fi
    printf '   /usr/bin/python3 %s/scripts/remote_input.py%s %s\n' \
      "$REPO_ROOT" "$(mac_arg)" "$ri_flags"
  fi

  say "install $LAUNCH_UNIT (button -> app launcher)"
  local button_cfg="$REPO_ROOT/config/button_launch.yaml"
  local button_example="$REPO_ROOT/config/button_launch.example.yaml"
  if [[ ! -f "$button_cfg" ]]; then
    say "create local button config from example"
    run "cp '$button_example' '$button_cfg'"
  fi
  if (( INSTALL_SERVICE )); then
    install_unit "$LAUNCH_UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user daemon-reload"; fi
    if systemctl --user is-active --quiet "$LAUNCH_UNIT" 2>/dev/null; then
      run "systemctl --user restart $LAUNCH_UNIT"
    else
      run "systemctl --user enable --now $LAUNCH_UNIT"
    fi
  else
    printf '   /usr/bin/python3 %s/scripts/remote_launch.py --config %s/config/button_launch.yaml\n' \
      "$REPO_ROOT" "$REPO_ROOT"
  fi
}

install_receiver() {
  say "install $RECEIVER_UNIT"
  if (( INSTALL_SERVICE )); then
    install_unit "$RECEIVER_UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user daemon-reload"; fi
  fi

  say "receiver config: $RECEIVER_CONFIG"
  if (( DRY )); then
    printf '   [dry-run] write %s and %s (compat: %s)\n' "$RECEIVER_CONFIG" "$RECEIVER_BIN" "$RECEIVER_COMPAT"
  else
    mkdir -p "$RECEIVER_CONFIG_DIR" "$(dirname "$RECEIVER_BIN")"
    if [[ ! -f "$RECEIVER_CONFIG" || "$FORCE_RECEIVER_CONFIG" -eq 1 ]]; then
      python3 - "$RECEIVER_CONFIG" "$RECEIVER_NAME" "$PIN_DISPLAY" <<'PY'
import json
import sys

path, name, pin_display = sys.argv[1:4]
cfg = {
    "name": name or None,
    "bind": None,
    "ssdp_port": 1900,
    "desc_port": 60000,
    "apps_port": 8009,
    "https_port": 8080,
    "https_bind": None,
    "voice_port": 9090,
    "pin_display": pin_display,
    "inject": "ydotool",
    "keymap": None,
    "voice_config": None,
    "cert_dir": None,
    "no_voice": False,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
      chmod 600 "$RECEIVER_CONFIG"
      say "  wrote $RECEIVER_CONFIG"
    else
      say "  keeping existing $RECEIVER_CONFIG"
    fi

    cat >"$RECEIVER_BIN" <<EOF
#!/bin/sh
export PYTHONPATH="$REPO_ROOT\${PYTHONPATH:+:\$PYTHONPATH}"
# Self-heal the project venv after a distro Python upgrade.
python3 "$REPO_ROOT/scripts/ensure_venv.py" --quiet || exit \$?
exec "$REPO_ROOT/.venv/bin/python" -m receiver.cli "\$@"
EOF
    chmod +x "$RECEIVER_BIN"
    ln -sfn "$RECEIVER_BIN" "$RECEIVER_COMPAT"
    say "  launcher: $RECEIVER_BIN (compat: $RECEIVER_COMPAT)"
  fi

  if [[ "$PIN_DISPLAY" != "none" ]] && ! python3 -c 'import PySide6' >/dev/null 2>&1; then
    cat >&2 <<'EOF'
note: PySide6 was not found, so the top-right PIN card will not appear.
      Pairing still works; the PIN is in the log and `firetv-receiver pin`.
      Fedora:        sudo dnf install python3-pyside6 layer-shell-qt
      Debian/Ubuntu: sudo apt install python3-pyside6.qml
      Arch:          sudo pacman -S pyside6 layer-shell-qt
      openSUSE:      sudo zypper install python3-pyside6 layer-shell-qt
      Alpine:        sudo apk add py3-pyside6
EOF
  fi

  if (( INSTALL_SERVICE )); then
    say "enable + start $RECEIVER_UNIT"
    # Let the systemd user manager pass the graphical session to the Qt card.
    run "systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE 2>/dev/null || true"
    if systemctl --user is-active --quiet "$RECEIVER_UNIT" 2>/dev/null; then
      run "systemctl --user restart $RECEIVER_UNIT"
    else
      run "systemctl --user enable --now $RECEIVER_UNIT"
    fi
  else
    say "no systemd: run the receiver manually:"
    printf '   %s run -v\n' "$RECEIVER_BIN"
  fi
}

purge_project_state() {
  say "remove project venv and generated keymap"
  run "rm -rf '$REPO_ROOT/.venv'"
  run "rm -f '$REPO_ROOT/.venv.lock' '$REPO_ROOT/config/keymap.json'"
}

if (( UNINSTALL )); then
  if (( RECEIVER_ONLY )); then
    say "stop + disable $RECEIVER_UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user disable --now $RECEIVER_UNIT 2>/dev/null"; fi
    run "rm -f '$HOME/.config/systemd/user/$RECEIVER_UNIT'"
    run "rm -f '$RECEIVER_BIN' '$RECEIVER_COMPAT'"
    if (( HAVE_SYSTEMD )); then run "systemctl --user daemon-reload"; fi
    if (( PURGE )); then
      say "remove $RECEIVER_CONFIG_DIR (pairings and TLS identity)"
      run "rm -rf '$RECEIVER_CONFIG_DIR'"
      purge_project_state
    fi
    say "done."
    exit 0
  fi

  if (( ! VOICE_ONLY )); then
    say "stop + disable $UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user disable --now $UNIT"; fi
    run "rm -f '$HOME/.config/systemd/user/$UNIT'"

    say "stop + disable $LAUNCH_UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user disable --now $LAUNCH_UNIT 2>/dev/null"; fi
    run "rm -f '$HOME/.config/systemd/user/$LAUNCH_UNIT' 2>/dev/null" || true
  fi

  say "stop + disable $VOICE_UNIT"
  if (( HAVE_SYSTEMD )); then run "systemctl --user disable --now $VOICE_UNIT 2>/dev/null"; fi
  run "rm -f '$HOME/.config/systemd/user/$VOICE_UNIT' 2>/dev/null" || true

  if (( RECEIVER )); then
    say "stop + disable $RECEIVER_UNIT"
    if (( HAVE_SYSTEMD )); then run "systemctl --user disable --now $RECEIVER_UNIT 2>/dev/null"; fi
    run "rm -f '$HOME/.config/systemd/user/$RECEIVER_UNIT' 2>/dev/null" || true
    run "rm -f '$RECEIVER_BIN' '$RECEIVER_COMPAT' 2>/dev/null" || true
    if (( PURGE )); then
      say "remove $RECEIVER_CONFIG_DIR (pairings and TLS identity)"
      run "rm -rf '$RECEIVER_CONFIG_DIR'"
    fi
  fi

  if (( PURGE )); then
    purge_project_state
  fi

  if (( HAVE_SYSTEMD )); then run "systemctl --user daemon-reload"; fi
  if (( ! VOICE_ONLY )) && (( IS_KDE )); then
    if [[ -f "$KG_BAK" ]] && grep -q "^$KG_ACTION=.*Menu" "$KG_BAK"; then
      kg_foreign "$KG_ACTION" '[16777301]'
    else
      kg_foreign "$KG_ACTION" '[]'
    fi
    if [[ -f "$KG_BAK" ]]; then
      say "restore kglobalshortcutsrc from backup"
      run "cp '$KG_BAK' '$KG'"
      run "rm -f '$KG_BAK'"
    fi
  fi
  say "done."
  exit 0
fi

if (( ! RECEIVER_ONLY )) && [[ -z "$REMOTE_MAC" ]]; then
  if REMOTE_MAC="$(detect_remote_mac)"; then
    say "detected connected Fire TV remote: $REMOTE_MAC"
  elif REMOTE_MAC="$(existing_remote_mac)"; then
    say "keeping remote address from the installed unit: $REMOTE_MAC"
  fi
fi
if (( ! RECEIVER_ONLY )) && [[ -z "$REMOTE_MAC" ]]; then
  echo "Remote not connected; units will auto-detect it on next start." >&2
  echo "  (run REMOTE_MAC=AA:BB:CC:DD:EE:FF bash install.sh to pin one)" >&2
fi

install_deps
write_portable_keymap

if (( RECEIVER_ONLY )); then
  install_receiver
elif (( VOICE_ONLY )); then
  install_voice
  (( RECEIVER )) && install_receiver
else
  # voice-stt owns WSS 9090 in the merged setup; free it before starting voice.
  if (( HAVE_SYSTEMD )) && (( RECEIVER )) && systemctl --user is-active --quiet "$RECEIVER_UNIT" 2>/dev/null; then
    say "stop $RECEIVER_UNIT so voice-stt can own WSS 9090"
    run "systemctl --user stop $RECEIVER_UNIT"
  fi
  install_buttons
  install_voice
  (( RECEIVER )) && install_receiver
fi

say "done."
if (( ! RECEIVER_ONLY )); then
  if (( INSTALL_SERVICE )); then
    cat <<EOF

   BLE remote:
     journalctl --user -u $VOICE_UNIT -f
     Hold the remote's voice button, speak, release. "open firefox" launches
     the app; anything else is typed into the focused window.
EOF
  else
    cat <<EOF

   BLE remote (run these in separate terminals, voice first):
     $REPO_ROOT/.venv/bin/python $REPO_ROOT/scripts/06_voice_stt.py$(mac_arg) --type --wss
     /usr/bin/python3 $REPO_ROOT/scripts/remote_input.py$(mac_arg) $([ "$CEC_OWNED" -eq 0 ] && echo --portable)
EOF
  fi
fi

if (( RECEIVER )); then
  if (( INSTALL_SERVICE )); then
    cat <<EOF

   Official phone app:
     firetv-remote doctor
     firetv-remote pair
     firetv-remote devices
     firetv-remote forget "iPhone"
     journalctl --user -u $RECEIVER_UNIT -f
EOF
  else
    cat <<EOF

   Official phone app (run in a terminal):
     firetv-remote doctor
     firetv-remote pair
     $REPO_ROOT/.local/bin/firetv-remote run -v
EOF
  fi
  cat <<EOF

   The red PIN card appears in the top-right when Qt is available; otherwise
   use 'firetv-remote pin'. Paired phones are stored in
   $RECEIVER_CONFIG_DIR/devices.json.
EOF
fi

cat <<EOF

   Voice commands are conservative: only short explicit commands with a known
   app target execute ("Open Firefox"). Longer sentences, clauses, unknown app
   names and "open ..." in the middle of dictation are typed instead.

   In the full install, voice-stt.service serves the BLE button and the phone
   app's WSS 9090 stream through one faster-whisper model and CommandHandler
   (scripts/voice_core.py). --phone-only runs a standalone receiver WSS.
EOF

if (( INSTALL_SERVICE )) && (( ! RECEIVER_ONLY )); then
  cat <<EOF

   VERIFY: bash verify.sh   (KDE only: checks the Menu path and Plasma bindings).
EOF
fi

if (( ! RECEIVER_ONLY )); then
  cat <<EOF

   Ownership:
EOF
  if (( CEC_OWNED )); then
    cat <<EOF
     remote-input.service handles Menu only. Volume/mute/power belong to
     fedora-cec-toolkit's cec-remote-observer.service; keep it enabled.
EOF
  else
    cat <<EOF
     No cec-remote-observer detected: remote_input runs with --portable and the
     portable keymap uses local media keys for volume/mute/power.
EOF
  fi
fi

if (( ! INSTALL_SERVICE )); then
  cat <<EOF

   Config: $RECEIVER_CONFIG
   Keymap overrides: $REPO_ROOT/config/keymap.json (if generated)
EOF
fi
