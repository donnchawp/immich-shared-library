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

    # Identity is shared: the group id copies verbatim. We only need to make
    # sure the target user has their own person row on each group, so Immich's
    # deleteEmptyGroups doesn't drop it and null these faces.
    #
    # Once per *distinct* group, not once per face: the same few people recur
    # across an asset's faces, and ensure_target_person costs two queries every
    # time it is asked.
    resolved: dict[UUID, UUID | None] = {}
    for group_id in {f["personGroupId"] for f in source_faces if f["personGroupId"] is not None}:
        # None means the source has no person row for the group; those faces
        # copy unassigned rather than pointing at a group that may vanish.
        resolved[group_id] = await ensure_target_person(
            conn, group_id, source_user_id, target_user_id,
        )

    # Deduplicate by bounding box before inserting. The NOT EXISTS below cannot
    # see rows inserted by its own statement, so two source faces sharing a box
    # would both land — where the per-face loop this replaces inserted the first
    # and then skipped the second.
    rows, seen = [], set()
    for face in source_faces:
        box = (
            face["boundingBoxX1"], face["boundingBoxY1"],
            face["boundingBoxX2"], face["boundingBoxY2"],
        )
        if box in seen:
            continue
        seen.add(box)
        rows.append((
            uuid4(), resolved.get(face["personGroupId"]),
            face["imageWidth"], face["imageHeight"], *box, face["isVisible"],
        ))

    # Two deliberate departures from copying the source row verbatim, both to
    # keep this copy out of Immich's facial recognition: no face_search row, and
    # sourceType 'manual' rather than the source's 'machine-learning'. Each is
    # necessary and neither is sufficient — see the docstring above, and "Why
    # copied faces are invisible to recognition" in CLAUDE.md for the full
    # reasoning.
    #
    # One statement for the whole asset. The NOT EXISTS keeps the per-face
    # check-and-insert atomicity that made this a loop in the first place.
    #
    # It ignores soft-deleted faces, matching cleanup_reassigned_faces, which
    # requires deletedAt IS NULL on both sides. The two have to agree on what
    # "a face already exists at this box" means: while this one counted trashed
    # faces and that one did not, a soft-deleted target face blocked the copy
    # here forever and was invisible to reconciliation there, so the target
    # simply lost the face with nothing reporting it.
    inserted = await conn.fetch(
        """
        INSERT INTO asset_face (
            id, "assetId", "personGroupId",
            "imageWidth", "imageHeight",
            "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
            "sourceType", "isVisible"
        )
        SELECT t.id, $1, t.person_group_id, t.image_width, t.image_height,
               t.x1, t.y1, t.x2, t.y2, 'manual', t.is_visible
        FROM unnest(
            $2::uuid[], $3::uuid[], $4::int[], $5::int[],
            $6::int[], $7::int[], $8::int[], $9::int[], $10::bool[]
        ) AS t(id, person_group_id, image_width, image_height, x1, y1, x2, y2, is_visible)
        WHERE NOT EXISTS (
            SELECT 1 FROM asset_face ex
            WHERE ex."assetId" = $1
              AND ex."deletedAt" IS NULL
              AND ex."boundingBoxX1" = t.x1
              AND ex."boundingBoxY1" = t.y1
              AND ex."boundingBoxX2" = t.x2
              AND ex."boundingBoxY2" = t.y2
        )
        RETURNING id, "personGroupId"
        """,
        target_asset_id, *(list(col) for col in zip(*rows)),
    )

    # Point each target person's feature photo at a face the target owns.
    # faceAssetId is still an FK to asset_face.id, so it must never reference
    # the source user's face row. One face per group: the first inserted, which
    # is what the per-face loop settled on too, since the second face in a group
    # found faceAssetId already set and non-dangling.
    #
    # No early return when nothing was inserted. person."faceAssetId" is
    # ON DELETE SET NULL, so deleting a target face nulls the pointer rather
    # than leaving it dangling — which means the UPDATE's IS NULL half is the
    # one that does the work here, and the NOT EXISTS half is belt and braces
    # against a schema change. Either way a person can sit with no feature face
    # while the face it should point at is already on the asset: the row was
    # created by ensure_target_person, which does not set faceAssetId, or the
    # face it had was deleted and re-copied on a pass this one did not run.
    # Building `feature` from `inserted` alone meant the repair could only run
    # on a pass that had just created a face, which is the pass least likely to
    # need it.
    #
    # The fallback query runs only when nothing was inserted, so the hot path
    # is unchanged, and only for groups this asset's source faces resolved to.
    feature: dict[UUID, UUID] = {}
    for row in inserted:
        if row["personGroupId"] is not None:
            feature.setdefault(row["personGroupId"], row["id"])

    if not inserted:
        groups = [g for g in resolved.values() if g is not None]
        if groups:
            existing = await conn.fetch(
                """
                SELECT DISTINCT ON ("personGroupId") "personGroupId", id
                FROM asset_face
                WHERE "assetId" = $1
                  AND "deletedAt" IS NULL
                  AND "personGroupId" = ANY($2::uuid[])
                ORDER BY "personGroupId", id
                """,
                target_asset_id, groups,
            )
            for row in existing:
                feature.setdefault(row["personGroupId"], row["id"])

    if feature:
        await conn.execute(
            """
            UPDATE person p SET "faceAssetId" = t.face_id
            FROM unnest($1::uuid[], $2::uuid[]) AS t(person_group_id, face_id)
            WHERE p."ownerId" = $3 AND p."personGroupId" = t.person_group_id AND (
                p."faceAssetId" IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM asset_face WHERE id = p."faceAssetId"
                )
            )
            """,
            list(feature.keys()), list(feature.values()), target_user_id,
        )

    logger.debug(
        "Synced %d faces for asset %s -> %s", len(inserted), source_asset_id, target_asset_id
    )
    return len(inserted)


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
    try:
        async with conn.transaction():
            return await sync_faces_for_asset(
                conn, source_asset_id, target_asset_id, source_user_id, target_user_id,
            )
    except Exception:
        # A savepoint can't survive a lost connection; see sync_engine._cleanup_step.
        if conn.is_closed():
            raise
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
    done_sources: list[UUID] = []
    done_target_users: list[UUID] = []
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
        done_sources.append(pair["source_asset_id"])
        done_target_users.append(pair["target_user_id"])
        total += count

    # One statement, not one per pair. A reassignment-only pass copies nothing
    # but still advances every pair it touched, so this is the common case and
    # it used to cost a round trip each — a source-side recognition reset puts
    # every synced pair in the window at once.
    if done_sources:
        await conn.execute(
            """
            UPDATE _face_sync_asset_map m SET synced_at = NOW()
            FROM unnest($1::uuid[], $2::uuid[]) AS w(source_asset_id, target_user_id)
            WHERE m.source_asset_id = w.source_asset_id
              AND m.target_user_id = w.target_user_id
            """,
            done_sources, done_target_users,
        )

    return total
