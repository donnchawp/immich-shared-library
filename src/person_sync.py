import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

from src.config import settings
from src.file_ops import validate_path_within_upload

logger = logging.getLogger(__name__)


def _hardlink_person_thumbnail(
    source_person_id: UUID,
    target_person_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
    source_thumbnail_path: str,
) -> str:
    """Hardlink a person's cropped face thumbnail from source to target user directory.

    Person thumbnails follow: /data/thumbs/{userId}/{personId[0:2]}/{personId[2:4]}/{personId}.jpeg
    Returns the new thumbnail path for the target person.
    """
    if not source_thumbnail_path:
        return ""

    upload_base = Path(settings.upload_location_mount)
    source = Path(source_thumbnail_path)

    try:
        validate_path_within_upload(source)
    except ValueError:
        logger.error("Source person thumbnail escapes upload directory: %s", source)
        return ""

    if not source.exists():
        logger.warning("Source person thumbnail does not exist: %s", source)
        return ""

    # Build target path using target user ID and target person ID
    # Use upload_base instead of traversing parent directories (avoids depth assumptions)
    pid = str(target_person_id)
    ext = source.suffix  # .jpeg
    target_dir = upload_base / "thumbs" / str(target_user_id) / pid[:2] / pid[2:4]
    target = target_dir / f"{pid}{ext}"

    try:
        validate_path_within_upload(target)
    except ValueError:
        logger.error("Target person thumbnail escapes upload directory: %s", target)
        return ""

    target_dir.mkdir(parents=True, exist_ok=True)

    if target.exists():
        logger.debug("Target person thumbnail already exists: %s", target)
    else:
        try:
            os.link(str(source), str(target))
            logger.debug("Hardlinked person thumbnail %s -> %s", source, target)
        except OSError as e:
            logger.error("Failed to hardlink person thumbnail: %s", e)
            return ""

    return str(target)


async def get_or_create_target_person(
    conn: asyncpg.Connection,
    source_person_id: UUID,
) -> UUID | None:
    """Find or create a mirrored person for User B.

    Uses an advisory lock on the source person ID to prevent duplicate person
    creation from concurrent transactions.

    Returns the target person ID, or None if source person doesn't exist.
    """
    # Check if we already have a mapping (fast path, no lock needed)
    existing = await conn.fetchrow(
        """
        SELECT target_person_id FROM _face_sync_person_map
        WHERE source_person_id = $1 AND target_user_id = $2
        """,
        source_person_id,
        UUID(settings.target_user_id),
    )
    if existing:
        return existing["target_person_id"]

    # Serialize person creation for this source person to prevent orphan duplicates
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1::text))",
        str(source_person_id),
    )

    # Re-check after acquiring lock (another transaction may have created it)
    existing = await conn.fetchrow(
        """
        SELECT target_person_id FROM _face_sync_person_map
        WHERE source_person_id = $1 AND target_user_id = $2
        """,
        source_person_id,
        UUID(settings.target_user_id),
    )
    if existing:
        return existing["target_person_id"]

    # Get source person details
    source = await conn.fetchrow("SELECT * FROM person WHERE id = $1", source_person_id)
    if source is None:
        logger.warning("Source person %s not found", source_person_id)
        return None

    # Create mirrored person for target user
    target_person_id = uuid4()
    target_user_id = UUID(settings.target_user_id)

    # Hardlink the person's cropped face thumbnail
    target_thumbnail = _hardlink_person_thumbnail(
        source_person_id=source_person_id,
        target_person_id=target_person_id,
        source_user_id=source["ownerId"],
        target_user_id=target_user_id,
        source_thumbnail_path=source["thumbnailPath"],
    )

    await conn.execute(
        """
        INSERT INTO person (id, "ownerId", name, "thumbnailPath", "isHidden", "birthDate", "faceAssetId", "isFavorite", color)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        target_person_id,
        target_user_id,
        source["name"],
        target_thumbnail,
        source["isHidden"],
        source["birthDate"],
        None,  # faceAssetId — set after face sync
        False,
        source["color"],
    )

    # Track the mapping
    await conn.execute(
        """
        INSERT INTO _face_sync_person_map (source_person_id, target_person_id, source_user_id, target_user_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (source_person_id, target_user_id) DO NOTHING
        """,
        source_person_id,
        target_person_id,
        UUID(settings.source_user_id),
        UUID(settings.target_user_id),
    )

    logger.info("Created mirrored person %s -> %s (name=%s)", source_person_id, target_person_id, source["name"])
    return target_person_id


async def sync_person_thumbnails(conn: asyncpg.Connection) -> int:
    """Sync person thumbnail paths from source to target.

    Handles cases where:
    - Target person has empty thumbnailPath but source has one (initial creation gap)
    - Source thumbnail was regenerated (path changed)
    """
    rows = await conn.fetch(
        """
        SELECT m.source_person_id, m.target_person_id,
               s."thumbnailPath" as source_thumb, s."ownerId" as source_owner_id,
               t."thumbnailPath" as target_thumb
        FROM _face_sync_person_map m
        JOIN person s ON s.id = m.source_person_id
        JOIN person t ON t.id = m.target_person_id
        WHERE s."thumbnailPath" != ''
          AND (t."thumbnailPath" = '' OR t."thumbnailPath" IS NULL)
        """,
    )

    count = 0
    for row in rows:
        target_thumb = _hardlink_person_thumbnail(
            source_person_id=row["source_person_id"],
            target_person_id=row["target_person_id"],
            source_user_id=row["source_owner_id"],
            target_user_id=UUID(settings.target_user_id),
            source_thumbnail_path=row["source_thumb"],
        )
        if target_thumb:
            await conn.execute(
                'UPDATE person SET "thumbnailPath" = $1 WHERE id = $2',
                target_thumb,
                row["target_person_id"],
            )
            logger.info("Updated person %s thumbnail", row["target_person_id"])
            count += 1

    return count


async def sync_person_names(conn: asyncpg.Connection) -> int:
    """Sync person name changes from source to target.

    Returns the number of names updated.
    """
    updated = await conn.fetch(
        """
        UPDATE person t
        SET name = s.name
        FROM _face_sync_person_map m
        JOIN person s ON s.id = m.source_person_id
        WHERE t.id = m.target_person_id
          AND t.name IS DISTINCT FROM s.name
        RETURNING t.id, s.name
        """,
    )

    for row in updated:
        logger.info("Updated person %s name to '%s'", row["id"], row["name"])

    return len(updated)


async def sync_person_visibility(conn: asyncpg.Connection) -> int:
    """Sync person isHidden changes from source to target."""
    updated = await conn.fetch(
        """
        UPDATE person t
        SET "isHidden" = s."isHidden"
        FROM _face_sync_person_map m
        JOIN person s ON s.id = m.source_person_id
        WHERE t.id = m.target_person_id
          AND t."isHidden" IS DISTINCT FROM s."isHidden"
        RETURNING t.id
        """,
    )
    return len(updated)


async def cleanup_orphaned_persons(conn: asyncpg.Connection) -> int:
    """Remove target persons whose source person has been deleted."""
    deleted = await conn.fetch(
        """
        DELETE FROM person t
        USING _face_sync_person_map m
        WHERE t.id = m.target_person_id
          AND NOT EXISTS (SELECT 1 FROM person WHERE id = m.source_person_id)
        RETURNING t.id
        """,
    )

    if deleted:
        await conn.execute(
            """
            DELETE FROM _face_sync_person_map
            WHERE target_person_id = ANY($1)
            """,
            [row["id"] for row in deleted],
        )
        logger.info("Cleaned up %d orphaned mirrored persons", len(deleted))

    return len(deleted)
