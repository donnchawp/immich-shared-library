#!/usr/bin/env bash
set -euo pipefail

SCRIPTS=("test_sync.py" "dedup_synced.py" "delete_synced.py")
NETWORK="immich_default"
IMAGE="python:3.12-slim"

usage() {
    echo "Usage: $0 <script> [args...]"
    echo ""
    echo "Run a utility script inside a Docker container on the Immich network."
    echo ""
    echo "Available scripts:"
    for s in "${SCRIPTS[@]}"; do
        echo "  $s"
    done
    echo ""
    echo "Examples:"
    echo "  $0 test_sync.py"
    echo "  $0 dedup_synced.py --match-time"
    echo "  $0 delete_synced.py"
    exit 1
}

if [[ $# -lt 1 ]]; then
    usage
fi

SCRIPT="$1"
shift

# Validate script name
valid=false
for s in "${SCRIPTS[@]}"; do
    if [[ "$SCRIPT" == "$s" ]]; then
        valid=true
        break
    fi
done
if [[ "$valid" == false ]]; then
    echo "Error: unknown script '$SCRIPT'"
    usage
fi

# Load volume paths from .env
ENV_FILE="$(dirname "$0")/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: .env not found. Copy env.example to .env and fill in your values."
    exit 1
fi

get_env() {
    local key="$1"
    local default="${2:-}"
    local val
    val=$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)
    echo "${val:-$default}"
}

UPLOAD_LOCATION=$(get_env UPLOAD_LOCATION)
EXTERNAL_LIBRARY_DIR=$(get_env EXTERNAL_LIBRARY_DIR)
UPLOAD_LOCATION_MOUNT=$(get_env UPLOAD_LOCATION_MOUNT "/usr/src/app/upload")
EXTERNAL_LIBRARY_MOUNT=$(get_env EXTERNAL_LIBRARY_MOUNT "/external_library")

if [[ -z "$UPLOAD_LOCATION" ]]; then
    echo "Error: UPLOAD_LOCATION not set in .env"
    exit 1
fi
if [[ -z "$EXTERNAL_LIBRARY_DIR" ]]; then
    echo "Error: EXTERNAL_LIBRARY_DIR not set in .env"
    exit 1
fi

echo "Running $SCRIPT in Docker container..."
echo "  Network: $NETWORK"
echo "  Upload:  $UPLOAD_LOCATION -> $UPLOAD_LOCATION_MOUNT"
echo "  Library: $EXTERNAL_LIBRARY_DIR -> $EXTERNAL_LIBRARY_MOUNT"
echo ""

exec docker run --rm -it --network "$NETWORK" \
    -v "$(cd "$(dirname "$0")" && pwd)":/app \
    -v "${UPLOAD_LOCATION}:${UPLOAD_LOCATION_MOUNT}" \
    -v "${EXTERNAL_LIBRARY_DIR}:${EXTERNAL_LIBRARY_MOUNT}" \
    -w /app "$IMAGE" \
    bash -c "pip install -q asyncpg httpx pydantic pydantic-settings pyyaml && python $SCRIPT $*"
