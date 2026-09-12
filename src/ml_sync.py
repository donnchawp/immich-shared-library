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

    Copies asset_face."personGroupId" verbatim -- identity is shared under
    cluster groups, so there is nothing to mirror -- and ensures the target
    user has their own person row on that group. Deliberately does NOT copy
    the embedding: a copied face gets no face_search row and sourceType
    'manual', which together keep it out of Immich's recognition. See "Why
    copied faces are invisible to recognition" in CLAUDE.md.

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

        # Two deliberate departures from copying the source row verbatim, both
        # to keep this copy out of Immich's facial recognition:
        #
        # No face_search row. searchFaces() inner-joins face_search, so a face
        # without an embedding is never a candidate. That matters because a copied
        # embedding is byte-identical to its source — a distance-0 twin — and
        # recognition counts matches to decide whether a cluster reaches minFaces.
        # Copying it made every shared face vote twice, so a person in two synced
        # photos cleared a threshold of 3 and became a person who should not exist.
        #
        # sourceType 'manual', not the source's 'machine-learning'. getAllFaces()
        # only queues machine-learning faces and handleRecognizeFaces() skips
        # anything else *before* it checks for an embedding, so this is what stops
        # the copies being queued and failing. It pairs with the missing embedding:
        # drop one without the other and a cluster-wide reset queues every copy,
        # each failing with "does not have an embedding".
        #
        # Not 'exif' — metadata extraction deletes every exif-sourced face on an
        # asset and rebuilds it from XMP regions, and the sidecar syncs XMP.
        #
        # The cost: the target's faces can no longer be recognized independently.
        # That is the design (the source is authoritative for identity), but it
        # means leaving the cluster group needs a face-detection re-run.
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
            "manual",
            face["isVisible"],
        )
        if result == "INSERT 0 0":
            continue

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
        # Advance the watermark on every pair that was processed without
        # error, not only on pairs that inserted a face. A source face
        # reassigned to a different person group bumps asset_face."updatedAt"
        # but copies nothing — the bounding box is already on the target — so
        # gating this on count > 0 left the pair permanently inside the
        # window, re-fetched and re-scanned on every cycle forever. The
        # reassignment itself is applied by cleanup_reassigned_faces in Phase
        # 4, which does not use this watermark.
        await conn.execute(
            "UPDATE _face_sync_asset_map SET synced_at = NOW() WHERE source_asset_id = $1 AND target_user_id = $2",
            pair["source_asset_id"],
            pair["target_user_id"],
        )
        total += count

    return total
