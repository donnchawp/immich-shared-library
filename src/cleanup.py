import logging

import asyncpg

from src.file_ops import owned_file_paths, remove_hardlinks

logger = logging.getLogger(__name__)


async def delete_target_asset(conn: asyncpg.Connection, target_asset_id) -> bool:
    """Delete one target asset: its hardlinks, its album rows, the asset, the mapping.

    Files go before records. If the unlink fails the DB records stay and the
    next cycle retries; if the DB delete fails afterwards, the hardlinks are
    merely orphan files, which is safe — the source still holds a link to the
    inode.

    Runs inside its own savepoint. Callers batch these in one transaction, and
    catching the exception is not enough on its own: a failed statement leaves
    Postgres in an aborted transaction, so without the rollback every later
    item dies with InFailedSQLTransactionError and the whole batch unwinds
    because of one bad asset.

    Returns True if the asset was deleted, False if it failed.
    """
    try:
        async with conn.transaction():
            # Paths we created and may delete — excludes the XMP sidecar,
            # which is not ours. See file_ops.owned_file_paths.
            file_paths = await owned_file_paths(conn, target_asset_id)
            remove_hardlinks(file_paths)

            # Belt and braces: album_asset."assetId" is already ON DELETE
            # CASCADE, and the album_asset_delete_audit trigger fires either
            # way (it is guarded on pg_trigger_depth() <= 1). Kept as a
            # statement of intent and against the FK changing, at the cost of
            # one round trip per deleted asset.
            await conn.execute(
                'DELETE FROM album_asset WHERE "assetId" = $1',
                target_asset_id,
            )

            # Cascades to exif, files, faces, smart_search, job_status
            await conn.execute("DELETE FROM asset WHERE id = $1", target_asset_id)

            await conn.execute(
                "DELETE FROM _face_sync_asset_map WHERE target_asset_id = $1",
                target_asset_id,
            )
        return True
    except Exception:
        logger.exception("Failed to delete target asset %s", target_asset_id)
        return False


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
        if await delete_target_asset(conn, target_id):
            logger.info(
                "Cleaned up deleted asset: source=%s target=%s",
                row["source_asset_id"], target_id,
            )
            count += 1

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

    It is also the last source-authoritative write left -- names are fill-only
    and visibility is not synced at all, both per-user by design in v3.2.0 --
    so the case for it stands on its own: the copies carry no embedding and
    sourceType 'manual', so they cannot re-cluster themselves out of a bad
    assignment, and the source is the only account whose recognition still runs
    on these faces.

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

    Cost, understood and accepted: this is a full scan of
    ``_face_sync_asset_map`` joined twice to ``asset_face``, every cycle,
    forever, and it does nothing almost every time. Unlike Phase 2 it has no
    watermark, and it must not grow one — a watermark records that work was
    done, and this function's whole job is to converge state that *something
    else* failed to apply, including a Phase 2 pass that advanced its own
    watermark before dying. A drift it skips once it would skip permanently.
    The cost is bounded by index lookups and grows linearly with the library;
    if that stops being acceptable the answer is a cheap pre-check (does any
    mapped source face have ``updatedAt`` past the last full pass?) gating the
    expensive statement, not a watermark on the statement itself.
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
