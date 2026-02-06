import logging

import asyncpg

from src.file_ops import remove_hardlinks
from src.person_sync import get_or_create_target_person

logger = logging.getLogger(__name__)


async def cleanup_deleted_assets(conn: asyncpg.Connection) -> int:
    """Remove target assets whose source has been deleted.

    Returns the number of assets cleaned up.
    """
    # Find synced assets where source no longer exists or is soft-deleted
    orphaned = await conn.fetch(
        """
        SELECT m.source_asset_id, m.target_asset_id
        FROM _face_sync_asset_map m
        LEFT JOIN asset a ON a.id = m.source_asset_id AND a."deletedAt" IS NULL
        WHERE a.id IS NULL
        """,
    )

    if not orphaned:
        return 0

    count = 0
    for row in orphaned:
        target_id = row["target_asset_id"]
        source_id = row["source_asset_id"]

        try:
            # Get file paths before deleting records
            files = await conn.fetch(
                'SELECT path FROM asset_file WHERE "assetId" = $1',
                target_id,
            )
            file_paths = [f["path"] for f in files]

            # Remove hardlinked files first — if this fails, DB records stay
            # and we can retry next cycle. If DB delete fails after file removal,
            # the hardlinks are just orphan files (safe, since they're hardlinks
            # and the source still has a link to the inode).
            remove_hardlinks(file_paths)

            # Remove from albums before deleting asset
            await conn.execute(
                'DELETE FROM album_asset WHERE "assetId" = $1',
                target_id,
            )

            # Delete the target asset (cascades to exif, files, faces, smart_search, job_status)
            await conn.execute("DELETE FROM asset WHERE id = $1", target_id)

            # Remove the mapping
            await conn.execute(
                "DELETE FROM _face_sync_asset_map WHERE target_asset_id = $1",
                target_id,
            )

            logger.info("Cleaned up deleted asset: source=%s target=%s", source_id, target_id)
            count += 1

        except Exception:
            logger.exception("Failed to clean up target asset %s", target_id)

    return count


async def cleanup_reassigned_faces(conn: asyncpg.Connection) -> int:
    """Handle person merges: when source faces are reassigned to different persons.

    Detects when a source face's personId no longer matches the expected mapping
    and updates the target face accordingly.
    Returns the number of faces updated.
    """
    # Find target faces where the source face's person has changed
    # User IDs are derived from the tracking table instead of global settings
    mismatched = await conn.fetch(
        """
        SELECT
            tf.id AS target_face_id,
            sf."personId" AS new_source_person_id,
            tf."personId" AS current_target_person_id,
            m.source_user_id,
            m.target_user_id
        FROM _face_sync_asset_map m
        JOIN asset_face sf ON sf."assetId" = m.source_asset_id AND sf."deletedAt" IS NULL
        JOIN asset_face tf ON tf."assetId" = m.target_asset_id AND tf."deletedAt" IS NULL
            AND tf."boundingBoxX1" = sf."boundingBoxX1"
            AND tf."boundingBoxY1" = sf."boundingBoxY1"
            AND tf."boundingBoxX2" = sf."boundingBoxX2"
            AND tf."boundingBoxY2" = sf."boundingBoxY2"
        LEFT JOIN _face_sync_person_map pm ON pm.source_person_id = sf."personId"
            AND pm.target_user_id = m.target_user_id
        WHERE tf."personId" IS DISTINCT FROM pm.target_person_id
        """,
    )

    count = 0
    for row in mismatched:
        new_target_person_id = None
        if row["new_source_person_id"] is not None:
            new_target_person_id = await get_or_create_target_person(
                conn, row["new_source_person_id"],
                row["source_user_id"], row["target_user_id"],
            )

        await conn.execute(
            'UPDATE asset_face SET "personId" = $1 WHERE id = $2',
            new_target_person_id,
            row["target_face_id"],
        )
        count += 1

    if count > 0:
        logger.info("Updated %d faces due to person reassignment", count)

    return count
