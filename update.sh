#!/usr/bin/env bash
# Update an existing user-level installation from this checkout.
#
#   ./update.sh              re-run the installer (idempotent)
#   ./update.sh --pull       git pull --ff-only first, then update
#   ./update.sh --dry-run    preview every change
#
# Existing config and pairings are kept:
#   config/keymap.json
#   ~/.config/firetv-receiver/config.json
#   ~/.config/firetv-receiver/devices.json
#   ~/.config/firetv-receiver/poc-tls/
#
# Pass any install.sh flag through (for example --mac, --name, --force-keymap).
# To remove the install, use ./uninstall.sh (add --purge for personal data).

set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PULL=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --pull) PULL=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done

if (( PULL )); then
  if [[ -d "$ROOT/.git" ]]; then
    echo "updating checkout: git pull --ff-only"
    git -C "$ROOT" pull --ff-only
  else
    echo "note: $ROOT is not a git checkout; skipping --pull" >&2
  fi
fi

if (( ${#ARGS[@]} )); then
  exec "$ROOT/install.sh" "${ARGS[@]}"
else
  exec "$ROOT/install.sh"
fi
