#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORB_VENV="${ORB_VENV:-$HOME/.venvs/ieee_download}"
ORB_LOG="${ORB_LOG:-$ROOT_DIR/jssc_orb_catchup.log}"
CREDENTIAL_FILE="${CREDENTIAL_FILE:-}"

if [ -n "$CREDENTIAL_FILE" ]; then
    if [ ! -f "$CREDENTIAL_FILE" ]; then
        echo "credential file not found: $CREDENTIAL_FILE" >&2
        exit 1
    fi
elif [ -z "${IEEE_INST_NAME:-}" ] || [ -z "${IEEE_INST_USERNAME:-}" ] || [ -z "${IEEE_INST_PASSWORD:-}" ]; then
    echo "Set CREDENTIAL_FILE or export IEEE_INST_NAME/IEEE_INST_USERNAME/IEEE_INST_PASSWORD before starting the worker." >&2
    exit 1
fi

if ! command -v xvfb-run >/dev/null 2>&1 || ! command -v tmux >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3-pip python3-venv xvfb tmux
fi

if [ ! -d "$ORB_VENV" ]; then
    python3 -m venv "$ORB_VENV"
fi

source "$ORB_VENV/bin/activate"
python -m pip install -q --upgrade pip
python -m pip install -q playwright==1.58.0
python -m playwright install --with-deps chromium

if [ -n "$CREDENTIAL_FILE" ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            ""|\#*)
                continue
                ;;
        esac
        export "$key=$value"
    done < "$CREDENTIAL_FILE"
fi

cd "$ROOT_DIR"
xvfb-run -a python -u jssc_container_catchup.py "$@" 2>&1 | tee -a "$ORB_LOG"
