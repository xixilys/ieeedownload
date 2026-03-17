#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${IEEE_DOWNLOAD_IMAGE:-ieeedownload-jssc:playwright-1.52.0}"
CREDENTIAL_DIR="${CREDENTIAL_DIR:-}"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker command not found" >&2
    exit 1
fi

if [ -z "$CREDENTIAL_DIR" ] || [ ! -d "$CREDENTIAL_DIR" ]; then
    echo "Set CREDENTIAL_DIR to a local directory that contains your credential file(s)." >&2
    exit 1
fi

if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    echo "Building Docker image $IMAGE_TAG ..."
    docker build -t "$IMAGE_TAG" -f "$ROOT_DIR/Dockerfile.jssc" "$ROOT_DIR"
fi

exec docker run --rm --init \
    --shm-size=1g \
    -v "$ROOT_DIR:/work" \
    -v "$CREDENTIAL_DIR:$CREDENTIAL_DIR:ro" \
    -w /work \
    "$IMAGE_TAG" \
    "$@"
