#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORB_MACHINE="${ORB_MACHINE:-ubuntu}"
ORB_WORKDIR="/mnt/mac${ROOT_DIR}"
ORB_SESSION="${ORB_SESSION:-jssc_catchup_orb}"

if ! command -v orb >/dev/null 2>&1; then
    echo "orb command not found" >&2
    exit 1
fi

printf -v FORWARDED_ARGS '%q ' "$@"

orb -m "$ORB_MACHINE" bash -lc "
set -euo pipefail
cd '$ORB_WORKDIR'
chmod +x ./jssc_orb_worker.sh
tmux kill-session -t '$ORB_SESSION' 2>/dev/null || true
tmux new-session -d -s '$ORB_SESSION' './jssc_orb_worker.sh ${FORWARDED_ARGS}'
echo 'Orb catch-up session started:' '$ORB_SESSION'
echo 'Log:' '$ORB_WORKDIR/jssc_orb_catchup.log'
"
