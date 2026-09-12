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

# ON_ERROR_STOP is load-bearing, not hygiene. Without it psql exits 0 after a
# failed statement, so `set -e` never fires and the script marches on to drop
# the tracking tables — leaving orphaned assets in Immich and destroying the
# only record of which ones they were.
psql_cmd() {
    docker exec -e PGPASSWORD="$DB_PASSWORD" "$POSTGRES_CONTAINER" \
        psql -v ON_ERROR_STOP=1 -U "$DB_USERNAME" -d "$DB_DATABASE_NAME" -t -A -c "$1"
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
# v3.2.0 cluster groups: there is no more person-mapping table. A
# sidecar-created person is identified structurally, the same way
# cleanup_orphaned_persons (src/person_sync.py) does it: a person row
# owned by a sidecar-managed target user, on a personGroupId that the
# paired source user (per _face_sync_asset_map) also has a person row
# for. This count is a preview only (matches the DELETE's scoping
# predicate below, but not its face-guard, since the guard's answer
# changes once assets are deleted a few steps from now).
person_count=$(psql_cmd "
    SELECT COUNT(*) FROM person t
    WHERE EXISTS (
        SELECT 1 FROM _face_sync_asset_map m
        JOIN person s ON s.\"personGroupId\" = t.\"personGroupId\"
                      AND s.\"ownerId\" = m.source_user_id
        WHERE m.target_user_id = t.\"ownerId\"
    )
" 2>/dev/null || echo "0")
skipped_count=$(psql_cmd "SELECT COUNT(*) FROM _face_sync_skipped" 2>/dev/null || echo "0")

echo "Database:"
echo "  Synced assets:    $asset_count"
echo "  Synced persons:   $person_count"
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
echo "  3. Delete up to $person_count synced person(s) from Immich"
echo "  4. Drop all sidecar tracking tables"
[[ ${#symlinks[@]} -gt 0 ]] && echo "  5. Remove ${#symlinks[@]} symlink(s)"
echo ""
# Not ${confirm,,}: that is bash 4+, and on a stock macOS host the shebang
# resolves to bash 3.2, where it is a bad-substitution error that kills the
# script here — after the preview, before anything destructive, so safe but
# unusable.
read -p "Proceed? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# Stop sidecar
echo ""
echo "Stopping sidecar..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true

# Delete synced assets (album entries first, then assets cascade to child tables)
#
# Note: this leaves the hardlinked thumbnail and preview files behind. They are
# hardlinks, so they cost no extra disk space, but nothing after this point
# knows their paths. delete_synced.py removes the files first and is the
# better tool if you want a clean filesystem; this is the blunt instrument.
if [[ "$asset_count" -gt 0 ]]; then
    echo "Deleting $asset_count synced asset(s)..."
    psql_cmd "
        DELETE FROM album_asset WHERE \"assetId\" IN (SELECT target_asset_id FROM _face_sync_asset_map);
        DELETE FROM asset WHERE id IN (SELECT target_asset_id FROM _face_sync_asset_map);
    "
fi

# Delete synced persons (runs after asset deletion above, so any faces that
# lived only on synced assets are already gone).
#
# Two guards, and it is the EXISTS that matters most. Requiring a mapped
# source to still hold a person row on the group is what keeps the group
# non-empty after this delete, so Immich's deleteEmptyGroups cannot drop it
# and unassign every face in it — the source user's own included.
#
# The NOT EXISTS is then free to be owner-scoped: it spares any target person
# still carrying faces from the target's own non-synced assets. Group-scoped,
# as in cleanup_orphaned_persons (src/person_sync.py), it would match the
# source's faces on the source's own photos and refuse every deletion here.
# Same predicate as delete_target_person_in_shared_group, which is where it
# is documented and tested; keep the two in step.
if [[ "$person_count" -gt 0 ]]; then
    echo "Deleting up to $person_count synced person(s)..."
    psql_cmd "
        DELETE FROM person t
        WHERE EXISTS (
            SELECT 1 FROM _face_sync_asset_map m
            JOIN person s ON s.\"personGroupId\" = t.\"personGroupId\"
                          AND s.\"ownerId\" = m.source_user_id
            WHERE m.target_user_id = t.\"ownerId\"
        )
        AND NOT EXISTS (
            SELECT 1 FROM asset_face af
            JOIN asset a ON a.id = af.\"assetId\"
            WHERE af.\"personGroupId\" = t.\"personGroupId\"
              AND a.\"ownerId\" = t.\"ownerId\"
              AND af.\"deletedAt\" IS NULL
        );
    "
fi

# Drop tracking tables. Only after both deletes above have succeeded —
# _face_sync_asset_map is the only record of which assets and persons this
# reset was supposed to remove, so dropping it on top of a failed delete
# leaves orphans that nothing can find again. set -e plus ON_ERROR_STOP
# already stops the script before here; this comment is why that matters.
#
# _face_sync_meta goes too: it holds the sidecar's own schema version, and a
# stale version pointing at tables that no longer exist would mislead the next
# migration run.
echo "Dropping tracking tables..."
psql_cmd "
    DROP TABLE IF EXISTS _face_sync_asset_map;
    DROP TABLE IF EXISTS _face_sync_person_map;
    DROP TABLE IF EXISTS _face_sync_skipped;
    DROP TABLE IF EXISTS _face_sync_meta;
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
echo "Reset complete. Run configure.py to reconfigure."
