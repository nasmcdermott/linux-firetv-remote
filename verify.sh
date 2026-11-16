#!/usr/bin/env bash
# Read-only check of the remote's Menu/power path (remote-input.service).
# Volume is verified by fedora-cec-toolkit's verify.sh; this only checks what
# this project owns. Sends nothing, changes nothing.
set -u
REPO="$(cd "$(dirname "$0")" && pwd)"
KG="$HOME/.config/kglobalshortcutsrc"
FAIL=0
ok() { echo "OK   $*"; }
bad() { echo "FAIL $*"; FAIL=$((FAIL+1)); }
warn() { echo "WARN $*"; }

UNIT_FILE="$HOME/.config/systemd/user/remote-input.service"
if [ -f "$UNIT_FILE" ]; then
  ok "remote-input.service installed"
  systemctl --user is-enabled remote-input.service >/dev/null 2>&1 && ok "remote-input.service enabled" || bad "remote-input.service not enabled (bash install.sh)"
  systemctl --user is-active remote-input.service >/dev/null 2>&1 && ok "remote-input.service active" || bad "remote-input.service not active"
  EXEC=$(sed -n 's/^ExecStart=.* \([^ ]*remote_input\.py\).*/\1/p' "$UNIT_FILE" | head -n1)
  EXEC="${EXEC/#%h/$HOME}"
  if [ -n "$EXEC" ] && [ -f "$EXEC" ]; then
    ok "unit runs $EXEC"
    [ "$EXEC" = "$REPO/scripts/remote_input.py" ] && ok "unit points at this checkout" || warn "unit runs a different checkout ($EXEC)"
  else
    bad "unit ExecStart script missing (reinstall: bash install.sh)"
  fi
else
  bad "remote-input.service not installed (bash install.sh)"
fi

SCRIPT="$REPO/scripts/remote_input.py"
if grep -v '^[[:space:]]*#' "$SCRIPT" | grep -Eq 'cec-policy.*"volume"|send_volume\(' ; then
  bad "remote_input.py dispatches volume (the observer owns it)"
else
  ok "remote_input.py does not dispatch volume"
fi
grep -v '^[[:space:]]*#' "$SCRIPT" | grep -q 'ydotool' \
  && bad "remote_input.py uses keystrokes (Menu must be D-Bus only)" \
  || ok "menu is D-Bus only (no keystroke injection)"

# One copy only: a second copy would send every Menu press twice (overlay toggles back off).
N_RI=$(pgrep -u "$(id -u)" -f '^/usr/bin/python3 .*remote_input\.py' | wc -l)
if [ "$N_RI" -le 1 ]; then ok "one remote_input.py process"; else bad "$N_RI remote_input.py processes (every Menu press is sent $N_RI times)"; fi
busctl --user introspect org.kde.plasmashell /PlasmaShell 2>/dev/null | grep -q activateLauncherMenu \
  && ok "plasmashell exposes activateLauncherMenu (the Menu action)" \
  || bad "plasmashell activateLauncherMenu unreachable (is Plasma running?)"

# Plasma's own binding for the Menu key would toggle the overlay a second time.
MENU_LIVE=$(gdbus call --session --dest=org.kde.kglobalaccel --object-path=/kglobalaccel \
  --method=org.kde.KGlobalAccel.getGlobalShortcutsByKey 16777301 2>/dev/null)
if [ -z "$MENU_LIVE" ]; then
  warn "could not query kglobalaccel (Plasma not running?)"
elif echo "$MENU_LIVE" | grep -q "activate widget 87"; then
  bad "Plasma binds Menu to the overlay live (double toggle). Reinstall: bash install.sh"
else
  ok "no live Plasma binding for the Menu key"
fi
if [ -f "$KG" ] && grep -q '^activate widget 87=.*Menu' "$KG"; then
  bad "kglobalshortcutsrc binds Menu to the overlay (applies at next login; reinstall: bash install.sh)"
else
  ok "kglobalshortcutsrc has no Menu binding for the overlay"
fi

if (cd "$REPO" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_remote_input.py >/dev/null 2>&1); then
  ok "remote_input unit tests pass"
else
  bad "remote_input unit tests fail (python3 -m unittest tests/test_remote_input.py)"
fi

echo "verify: $([ $FAIL -eq 0 ] && echo PASS || echo "FAIL ($FAIL)")"
exit $FAIL
