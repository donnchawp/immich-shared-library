import logging

import asyncpg

from src.file_ops import owned_file_paths, remove_hardlinks

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

        # Per-asset savepoint, same pattern as sync_asset. Catching the
        # exception is not enough on its own: a failed statement leaves
        # Postgres in an aborted transaction, so without the rollback every
        # later iteration dies with InFailedSQLTransactionError and the whole
        # cleanup unwinds because of one bad asset.
        await conn.execute("SAVEPOINT cleanup_asset")
        try:
            # Paths we created and may delete — excludes the XMP sidecar,
            # which is not ours. See file_ops.owned_file_paths.
            file_paths = await owned_file_paths(conn, target_id)

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

            await conn.execute("RELEASE SAVEPOINT cleanup_asset")
            logger.info("Cleaned up deleted asset: source=%s target=%s", source_id, target_id)
            count += 1

        except Exception:
            await conn.execute("ROLLBACK TO SAVEPOINT cleanup_asset")
            logger.exception("Failed to clean up target asset %s", target_id)

    return count


async def cleanup_stale_mappings(conn: asyncpg.Connection) -> int:
    """Prune mappings whose target asset no longer exists in Immich.

    When a target user hard-deletes a synced asset (e.g. empties the trash),
    the leftover mapping would otherwise block that source asset from ever
    re-syncing. Trashed assets still have a row (with deletedAt set), so
    their mapping survives until the trash is emptied — restoring from
    trash keeps the original mapping intact.

    Returns the number of mappings pruned; pruned sources re-sync in the
    next cycle's Phase 1.
    """
    pruned = await conn.fetch(
        """
        DELETE FROM _face_sync_asset_map m
        WHERE NOT EXISTS (SELECT 1 FROM asset a WHERE a.id = m.target_asset_id)
        RETURNING source_asset_id, target_asset_id
        """,
    )
    for row in pruned:
        logger.info(
            "Pruned stale mapping (target deleted in Immich): source=%s target=%s",
            row["source_asset_id"], row["target_asset_id"],
        )
    return len(pruned)


async def cleanup_reassigned_faces(conn: asyncpg.Connection) -> int:
    """Propagate source-side face reassignment to the target copy.

    Under cluster groups the person group id is shared, so this is a straight
    copy — no mapping table, no canonical resolution, no mirror-of-mirror loop.
    Matching is by exact bounding box, which is how the face was copied in the
    first place.

    The UPDATE is idempotent: once the target matches the source the WHERE
    clause stops selecting it, so repeated cycles converge.

    The source is authoritative. If the target user reassigns a copied face
    to a different person themselves, this reverts it on the next cycle,
    because the bounding box still matches and the group ids now differ
    again. That is intentional, not a bug.

    It is also the last source-authoritative write left. Names are fill-only
    and visibility is not synced at all, both per-user by design in v3.2.0, so
    the old justification here -- "the same principle as name/visibility sync"
    -- no longer holds and has been removed rather than quietly left to rot.
    The case for keeping this one is narrower and worth stating plainly: the
    copies carry no embedding and sourceType 'manual', so they cannot
    re-cluster themselves out of a bad assignment, and the source is the only
    account whose recognition still runs on these faces.

    Known limitations, left unhandled because they are edge cases rather than
    correctness defects:
    - If the source asset has two faces with identical bounding boxes,
      Postgres picks one arbitrarily for the join.
    - If the target user edits a copied face's bounding box, the match fails
      on every future cycle and that face is never reconciled again.

    Like ``sync_faces_for_asset`` (src/ml_sync.py), this refuses to point a
    target face at a group the target user has no ``person`` row on: with no
    row, Immich's ``deleteEmptyGroups`` can drop the group and null the face.
    Phase 2 normally creates that row via ``ensure_target_person``, but Phase 2
    runs in its own transaction (src/sync_engine.py) and may have failed, so
    the guard is stated here rather than assumed. An unassignment
    (``sf."personGroupId" IS NULL``) needs no person row and still propagates.
    """
    updated = await conn.fetch(
        """
        UPDATE asset_face tf
        SET "personGroupId" = sf."personGroupId"
        FROM _face_sync_asset_map m
        JOIN asset_face sf ON sf."assetId" = m.source_asset_id AND sf."deletedAt" IS NULL
        WHERE tf."assetId" = m.target_asset_id
          AND tf."deletedAt" IS NULL
          AND tf."boundingBoxX1" = sf."boundingBoxX1"
          AND tf."boundingBoxY1" = sf."boundingBoxY1"
          AND tf."boundingBoxX2" = sf."boundingBoxX2"
          AND tf."boundingBoxY2" = sf."boundingBoxY2"
          AND tf."personGroupId" IS DISTINCT FROM sf."personGroupId"
          AND (
              sf."personGroupId" IS NULL
              OR EXISTS (
                  SELECT 1 FROM person p
                  WHERE p."ownerId" = m.target_user_id
                    AND p."personGroupId" = sf."personGroupId"
              )
          )
        RETURNING tf.id, tf."personGroupId"
        """,
    )

    if updated:
        logger.info("Reassigned %d target faces to match source", len(updated))

    return len(updated)
