#!/usr/bin/env bash
# OPTIONAL: install the systemd *user* service (no root). Asks before changing anything.
# It only copies the unit file and reloads systemd; it does not start or enable it.
set -euo pipefail

UNIT="oci-a1-capacity-retry.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found: this system does not use systemd." >&2
    exit 1
fi
EXE="$HOME/.local/bin/oci-a1-retry"
if [ ! -x "$EXE" ]; then
    EXE="$(command -v oci-a1-retry || true)"
fi
if [ -z "$EXE" ]; then
    echo "oci-a1-retry is not installed; run scripts/install.sh first." >&2
    exit 1
fi

cat <<EOF
This will:
  - write $TARGET_DIR/$UNIT (ExecStart=$EXE run)
  - run: systemctl --user daemon-reload
It will NOT start or enable the service.
EOF
read -r -p "Continue? [y/N] " answer
case "$answer" in
[yY]*) ;;
*)
    echo "Nothing changed."
    exit 0
    ;;
esac

mkdir -p "$TARGET_DIR"
ESCAPED_EXE="$(printf '%s' "$EXE" | sed 's/[&|\\]/\\&/g')"
sed "s|^ExecStart=.*|ExecStart=$ESCAPED_EXE run|" "$REPO_DIR/packaging/systemd/$UNIT" >"$TARGET_DIR/$UNIT"
systemctl --user daemon-reload

cat <<EOF
Installed $TARGET_DIR/$UNIT

  systemctl --user start oci-a1-capacity-retry      # start now
  systemctl --user status oci-a1-capacity-retry     # state
  journalctl --user -u oci-a1-capacity-retry -f     # follow output
  systemctl --user stop oci-a1-capacity-retry       # stop (the OCI job is not canceled)
  systemctl --user enable oci-a1-capacity-retry     # start automatically at login
  loginctl enable-linger "$USER"                    # optional: also run while logged out
EOF
