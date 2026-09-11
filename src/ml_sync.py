import logging
from uuid import UUID, uuid4

import asyncpg

from src.person_sync import ensure_target_person

logger = logging.getLogger(__name__)


async def sync_faces_for_asset(
    conn: asyncpg.Connection,
    source_asset_id: UUID,
    target_asset_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
) -> int:
    """Copy face data from source asset to target asset.

    Creates mirrored persons as needed and copies face embeddings.
    Returns the number of faces synced.
    """
    # Get all faces on the source asset (including soft-deleted check)
    source_faces = await conn.fetch(
        """
        SELECT * FROM asset_face
        WHERE "assetId" = $1 AND "deletedAt" IS NULL
        """,
        source_asset_id,
    )

    if not source_faces:
        return 0

    count = 0
    for face in source_faces:
        source_face_id = face["id"]
        person_group_id = face["personGroupId"]

        # Identity is shared: the group id copies verbatim. We only need to make
        # sure the target user has their own person row on that group, so
        # Immich's deleteEmptyGroups doesn't drop it and null these faces.
        if person_group_id is not None:
            if await ensure_target_person(
                conn, person_group_id, source_user_id, target_user_id,
            ) is None:
                # Source has no person row for this group; copy the face
                # unassigned rather than pointing at a group that may vanish.
                person_group_id = None

        # Insert face record only if no matching bounding box exists on the target
        # asset (atomic check-and-insert to avoid TOCTOU race)
        target_face_id = uuid4()
        result = await conn.execute(
            """
            INSERT INTO asset_face (
                id, "assetId", "personGroupId",
                "imageWidth", "imageHeight",
                "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
                "sourceType", "isVisible"
            )
            SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
            WHERE NOT EXISTS (
                SELECT 1 FROM asset_face
                WHERE "assetId" = $2
                  AND "boundingBoxX1" = $6
                  AND "boundingBoxY1" = $7
                  AND "boundingBoxX2" = $8
                  AND "boundingBoxY2" = $9
            )
            """,
            target_face_id,
            target_asset_id,
            person_group_id,
            face["imageWidth"],
            face["imageHeight"],
            face["boundingBoxX1"],
            face["boundingBoxY1"],
            face["boundingBoxX2"],
            face["boundingBoxY2"],
            face["sourceType"],
            face["isVisible"],
        )
        if result == "INSERT 0 0":
            continue

        # Copy face embedding
        await conn.execute(
            """
            INSERT INTO face_search ("faceId", embedding)
            SELECT $1, embedding
            FROM face_search
            WHERE "faceId" = $2
            ON CONFLICT ("faceId") DO NOTHING
            """,
            target_face_id,
            source_face_id,
        )

        # Point the target person's feature photo at a face the target owns.
        # faceAssetId is still an FK to asset_face.id, so it must never
        # reference the source user's face row.
        if person_group_id is not None:
            await conn.execute(
                """
                UPDATE person SET "faceAssetId" = $1
                WHERE "ownerId" = $2 AND "personGroupId" = $3 AND (
                    "faceAssetId" IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM asset_face WHERE id = person."faceAssetId"
                    )
                )
                """,
                target_face_id,
                target_user_id,
                person_group_id,
            )

        count += 1

    if count > 0:
        logger.debug("Synced %d faces for asset %s -> %s", count, source_asset_id, target_asset_id)

    return count


async def sync_faces_for_asset_guarded(
    conn: asyncpg.Connection,
    source_asset_id: UUID,
    target_asset_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
) -> int | None:
    """sync_faces_for_asset under its own savepoint.

    Returns the number of faces synced, or None if the copy failed. All this
    guarantees is that the surrounding transaction is still usable afterwards;
    what a failure *means* — retry now, retry later, give up — is the caller's
    to decide, because it differs per phase.

    The guard is needed because catching the exception is not sufficient on
    its own: a failed statement leaves Postgres in an aborted transaction, and
    every later statement in it fails too. Unguarded, one bad asset takes the
    other 499 in a Phase 1 batch down with it, and in Phase 2 it kills the
    rest of the pass — then does so again every cycle, since a pair that
    failed keeps its old watermark and stays in the window.
    """
    await conn.execute("SAVEPOINT sync_faces")
    try:
        count = await sync_faces_for_asset(
            conn, source_asset_id, target_asset_id, source_user_id, target_user_id,
        )
        await conn.execute("RELEASE SAVEPOINT sync_faces")
        return count
    except Exception:
        await conn.execute("ROLLBACK TO SAVEPOINT sync_faces")
        logger.exception("Failed to sync faces for asset %s", source_asset_id)
        return None


async def sync_faces_incremental(conn: asyncpg.Connection) -> int:
    """Sync new or updated faces on already-synced assets.

    Only checks assets where source faces have been modified since the last sync.
    Returns total faces synced.
    """
    # Find synced asset pairs where the source has faces updated after synced_at.
    #
    # The target-asset guard is belt and braces. Phase 0 prunes mappings whose
    # target asset is gone before any phase reads the map, so this should find
    # nothing — but a target deleted mid-cycle would still land here, and
    # inserting a face for a vanished asset raises ForeignKeyViolationError.
    # That used to be fatal rather than merely annoying: the prune ran at the
    # end of Phase 4, so the abort killed the cycle before the fix could run,
    # every cycle, permanently. Keep the guard; it is now cheap insurance
    # rather than the only thing holding the cycle together.
    pairs = await conn.fetch(
        """
        SELECT m.source_asset_id, m.target_asset_id, m.synced_at,
               m.source_user_id, m.target_user_id
        FROM _face_sync_asset_map m
        WHERE EXISTS (
            SELECT 1 FROM asset_face af
            WHERE af."assetId" = m.source_asset_id
              AND af."updatedAt" > m.synced_at
              AND af."deletedAt" IS NULL
        )
          AND EXISTS (
            SELECT 1 FROM asset ta WHERE ta.id = m.target_asset_id
        )
        """,
    )

    total = 0
    for pair in pairs:
        count = await sync_faces_for_asset_guarded(
            conn, pair["source_asset_id"], pair["target_asset_id"],
            pair["source_user_id"], pair["target_user_id"],
        )
        if count is None:
            # Leave the watermark alone: the pair is still inside the window,
            # so the next cycle picks it up again without any extra state.
            continue
        if count > 0:
            # Update the watermark so we don't re-check this asset next cycle
            await conn.execute(
                "UPDATE _face_sync_asset_map SET synced_at = NOW() WHERE source_asset_id = $1 AND target_user_id = $2",
                pair["source_asset_id"],
                pair["target_user_id"],
            )
        total += count

    return total
