#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: .env not found."
    exit 1
fi

get_env() {
    local key="$1"
    local default="${2:-}"
    local val
    val=$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)
    echo "${val:-$default}"
}

DB_USERNAME=$(get_env DB_USERNAME postgres)
DB_PASSWORD=$(get_env DB_PASSWORD)
DB_DATABASE_NAME=$(get_env DB_DATABASE_NAME immich)
EXTERNAL_LIBRARY_DIR=$(get_env EXTERNAL_LIBRARY_DIR)
POSTGRES_CONTAINER="immich_postgres"

psql_cmd() {
    docker exec -e PGPASSWORD="$DB_PASSWORD" "$POSTGRES_CONTAINER" \
        psql -U "$DB_USERNAME" -d "$DB_DATABASE_NAME" -t -A -c "$1"
}

echo "=== Immich Shared Library Reset ==="
echo ""

# Check Postgres is reachable
if ! docker exec "$POSTGRES_CONTAINER" pg_isready -q 2>/dev/null; then
    echo "Error: Cannot reach $POSTGRES_CONTAINER. Is the Immich stack running?"
    exit 1
fi

# Gather counts
asset_count=$(psql_cmd "SELECT COUNT(*) FROM _face_sync_asset_map" 2>/dev/null || echo "0")
person_count=$(psql_cmd "SELECT COUNT(*) FROM _face_sync_person_map" 2>/dev/null || echo "0")
skipped_count=$(psql_cmd "SELECT COUNT(*) FROM _face_sync_skipped" 2>/dev/null || echo "0")

echo "Database:"
echo "  Synced assets:    $asset_count"
echo "  Mirrored persons: $person_count"
echo "  Skipped records:  $skipped_count"

# Find symlinks in external library
symlinks=()
if [[ -n "$EXTERNAL_LIBRARY_DIR" && -d "$EXTERNAL_LIBRARY_DIR" ]]; then
    while IFS= read -r link; do
        [[ -n "$link" ]] && symlinks+=("$link")
    done < <(find "$EXTERNAL_LIBRARY_DIR" -type l 2>/dev/null)
fi

echo ""
echo "Symlinks in $EXTERNAL_LIBRARY_DIR:"
if [[ ${#symlinks[@]} -eq 0 ]]; then
    echo "  (none)"
else
    for link in "${symlinks[@]}"; do
        echo "  $link -> $(readlink "$link")"
    done
fi

echo ""
echo "This will:"
echo "  1. Stop the sidecar container"
echo "  2. Delete $asset_count synced asset(s) from Immich"
echo "  3. Delete $person_count mirrored person(s) from Immich"
echo "  4. Drop all sidecar tracking tables"
[[ ${#symlinks[@]} -gt 0 ]] && echo "  5. Remove ${#symlinks[@]} symlink(s)"
echo ""
read -p "Proceed? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
    echo "Aborted."
    exit 0
fi

# Stop sidecar
echo ""
echo "Stopping sidecar..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true

# Delete synced assets (album entries first, then assets cascade to child tables)
if [[ "$asset_count" -gt 0 ]]; then
    echo "Deleting $asset_count synced asset(s)..."
    psql_cmd "
        DELETE FROM album_asset WHERE \"assetId\" IN (SELECT target_asset_id FROM _face_sync_asset_map);
        DELETE FROM asset WHERE id IN (SELECT target_asset_id FROM _face_sync_asset_map);
    "
fi

# Delete mirrored persons
if [[ "$person_count" -gt 0 ]]; then
    echo "Deleting $person_count mirrored person(s)..."
    psql_cmd "
        DELETE FROM person WHERE id IN (SELECT target_person_id FROM _face_sync_person_map);
    "
fi

# Drop tracking tables
echo "Dropping tracking tables..."
psql_cmd "
    DROP TABLE IF EXISTS _face_sync_asset_map;
    DROP TABLE IF EXISTS _face_sync_person_map;
    DROP TABLE IF EXISTS _face_sync_skipped;
"

# Remove symlinks
if [[ ${#symlinks[@]} -gt 0 ]]; then
    echo "Removing symlinks..."
    for link in "${symlinks[@]}"; do
        rm "$link"
        echo "  Removed $link"
    done
fi

echo ""
echo "Reset complete. Run setup.py to reconfigure."
