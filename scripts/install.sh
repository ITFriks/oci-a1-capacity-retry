#!/usr/bin/env bash
# Install oci-a1-capacity-retry for the current user. No sudo, no system-wide changes.
#
#   bash scripts/install.sh          # pipx if available, else a private virtualenv
#   bash scripts/install.sh --venv   # always use the private virtualenv
#
# Installs the 'oci-a1-retry' command into ~/.local/bin, creates
# ~/.config/oci-a1-capacity-retry/config.toml from the example if missing,
# then runs 'oci-a1-retry doctor'.
set -euo pipefail

APP="oci-a1-capacity-retry"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
BIN_DIR="$HOME/.local/bin"
USE_VENV=0
[ "${1:-}" = "--venv" ] && USE_VENV=1

find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON="$(find_python)"; then
    echo "Python 3.11 or newer was not found." >&2
    echo "Install it, e.g. on Ubuntu/Debian: sudo apt install python3 python3-venv pipx" >&2
    exit 1
fi
echo "Using $PYTHON ($("$PYTHON" --version 2>&1))"

if [ "$USE_VENV" = 0 ] && command -v pipx >/dev/null 2>&1; then
    echo "Installing with pipx..."
    pipx install --force --python "$(command -v "$PYTHON")" "$REPO_DIR"
    EXE="${PIPX_BIN_DIR:-$BIN_DIR}/oci-a1-retry"
else
    VENV="$DATA_DIR/venv"
    echo "Installing into a private virtual environment: $VENV"
    if ! "$PYTHON" -m venv --clear "$VENV"; then
        echo "Creating the virtual environment failed." >&2
        echo "On Ubuntu/Debian install the venv module: sudo apt install python3-venv" >&2
        exit 1
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet "$REPO_DIR"
    mkdir -p "$BIN_DIR"
    ln -sf "$VENV/bin/oci-a1-retry" "$BIN_DIR/oci-a1-retry"
    EXE="$BIN_DIR/oci-a1-retry"
fi
echo "Installed: $EXE"
case ":$PATH:" in
*":$BIN_DIR:"*) ;;
*) echo "NOTE: $BIN_DIR is not on your PATH. Add it, e.g.: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
esac

CONFIG_FILE="$CONFIG_DIR/config.toml"
if [ -f "$CONFIG_FILE" ]; then
    echo "Keeping existing config: $CONFIG_FILE"
else
    mkdir -p "$CONFIG_DIR"
    cp "$REPO_DIR/config.example.toml" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    echo "Created $CONFIG_FILE - edit it and set stack_ocid."
fi

cat <<EOF

OCI authentication
------------------
This tool uses the standard OCI config file: ~/.oci/config (profile DEFAULT unless
you pass --profile). It never stores credentials itself. If you do not have one yet:
  * OCI Console > Profile (top right) > My profile > API keys > Add API key,
    download the private key to ~/.oci/oci_api_key.pem (chmod 600) and paste the
    shown snippet into ~/.oci/config, setting key_file=~/.oci/oci_api_key.pem
  * or, with the OCI CLI installed: oci setup config
Your user needs IAM permission to 'read orm-stacks' and 'manage orm-jobs'.

Running 'oci-a1-retry doctor' now:
EOF
"$EXE" doctor || echo "(doctor reported problems - fix them, then run: oci-a1-retry doctor)"

cat <<EOF

Next steps:
  oci-a1-retry doctor     # all checks should pass
  oci-a1-retry status     # read-only
  oci-a1-retry once       # one real Apply attempt
  oci-a1-retry run        # retry until the Apply succeeds
Optional background service: bash scripts/install-systemd-user.sh
EOF
