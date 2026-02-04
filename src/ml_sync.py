import logging
from uuid import UUID, uuid4

import asyncpg

from src.person_sync import get_or_create_target_person

logger = logging.getLogger(__name__)


async def sync_faces_for_asset(
    conn: asyncpg.Connection,
    source_asset_id: UUID,
    target_asset_id: UUID,
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

        # Skip if we already have a face on the target asset with matching bounding box
        already_exists = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM asset_face
                WHERE "assetId" = $1
                  AND "boundingBoxX1" = $2
                  AND "boundingBoxY1" = $3
                  AND "boundingBoxX2" = $4
                  AND "boundingBoxY2" = $5
            )
            """,
            target_asset_id,
            face["boundingBoxX1"],
            face["boundingBoxY1"],
            face["boundingBoxX2"],
            face["boundingBoxY2"],
        )
        if already_exists:
            continue

        # Get or create mirrored person
        target_person_id = None
        if face["personId"] is not None:
            target_person_id = await get_or_create_target_person(conn, face["personId"])

        # Create face record for target asset
        target_face_id = uuid4()
        await conn.execute(
            """
            INSERT INTO asset_face (
                id, "assetId", "personId",
                "imageWidth", "imageHeight",
                "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
                "sourceType", "isVisible"
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            target_face_id,
            target_asset_id,
            target_person_id,
            face["imageWidth"],
            face["imageHeight"],
            face["boundingBoxX1"],
            face["boundingBoxY1"],
            face["boundingBoxX2"],
            face["boundingBoxY2"],
            face["sourceType"],
            face["isVisible"],
        )

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

        # Set faceAssetId on the target person if not set or if the
        # currently referenced face no longer exists
        if target_person_id is not None:
            await conn.execute(
                """
                UPDATE person SET "faceAssetId" = $1
                WHERE id = $2 AND (
                    "faceAssetId" IS NULL
                    OR NOT EXISTS (SELECT 1 FROM asset_face WHERE id = person."faceAssetId")
                )
                """,
                target_face_id,
                target_person_id,
            )

        count += 1

    if count > 0:
        logger.debug("Synced %d faces for asset %s -> %s", count, source_asset_id, target_asset_id)

    return count


async def sync_faces_incremental(conn: asyncpg.Connection) -> int:
    """Sync new or updated faces on already-synced assets.

    Only checks assets where source faces have been modified since the last sync.
    Returns total faces synced.
    """
    # Find synced asset pairs where the source has faces updated after synced_at
    pairs = await conn.fetch(
        """
        SELECT m.source_asset_id, m.target_asset_id, m.synced_at
        FROM _face_sync_asset_map m
        WHERE EXISTS (
            SELECT 1 FROM asset_face af
            WHERE af."assetId" = m.source_asset_id
              AND af."updatedAt" > m.synced_at
              AND af."deletedAt" IS NULL
        )
        """,
    )

    total = 0
    for pair in pairs:
        count = await sync_faces_for_asset(
            conn, pair["source_asset_id"], pair["target_asset_id"]
        )
        if count > 0:
            # Update the watermark so we don't re-check this asset next cycle
            await conn.execute(
                "UPDATE _face_sync_asset_map SET synced_at = NOW() WHERE source_asset_id = $1",
                pair["source_asset_id"],
            )
        total += count

    return total
