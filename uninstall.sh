#!/usr/bin/env bash
# Remove the user-level installation.
#
#   ./uninstall.sh            stop/remove units, launcher and generated keymap
#   ./uninstall.sh --purge    also remove receiver config/devices/TLS and .venv
#   ./uninstall.sh --dry-run  show what would be removed
#
# The source checkout is never deleted. This is a thin wrapper around
# `install.sh --uninstall`; pass the same flags.

set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT/install.sh" --uninstall "$@"
