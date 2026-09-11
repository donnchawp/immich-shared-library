import logging
import os
from pathlib import Path
from uuid import UUID

import asyncpg

from src.config import settings
from src.file_ops import validate_path_within_upload

logger = logging.getLogger(__name__)


def _hardlink_person_thumbnail(
    person_group_id: UUID,
    target_user_id: UUID,
    source_thumbnail_path: str,
) -> str:
    """Hardlink a person's cropped face thumbnail into the target user's directory.

    Since v3.2.0 person thumbnails are keyed on the *person group*, not the
    person: /data/thumbs/{ownerId}/{pgid[0:2]}/{pgid[2:4]}/{pgid}.jpeg
    Source and target share the group id, so only the owner directory changes.
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

    pgid = str(person_group_id)
    target_dir = upload_base / "thumbs" / str(target_user_id) / pgid[:2] / pgid[2:4]
    target = target_dir / f"{pgid}{source.suffix}"

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


async def ensure_target_person(
    conn: asyncpg.Connection,
    person_group_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
) -> UUID | None:
    """Ensure the target user has a ``person`` row for this shared group.

    Under cluster groups the identity itself (``person_group``) is shared, so
    there is nothing to mirror — only the target's own name/thumbnail row to
    create. Immich creates this row lazily during facial recognition
    (person.service.ts:548), which the sidecar deliberately skips, so we must
    create it ourselves. Without it Immich's ``deleteEmptyGroups`` would later
    drop the group and null the copied faces.

    Returns ``person_group_id`` on success, or ``None`` if the source user has
    no person row for this group (nothing to copy a name from).
    """
    source = await conn.fetchrow(
        'SELECT name, "thumbnailPath", "isHidden", "birthDate", color '
        'FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        source_user_id,
        person_group_id,
    )
    if source is None:
        logger.debug(
            "Source user %s has no person row for group %s", source_user_id, person_group_id
        )
        return None

    already = await conn.fetchval(
        'SELECT 1 FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        target_user_id,
        person_group_id,
    )
    if already:
        return person_group_id

    target_thumbnail = _hardlink_person_thumbnail(
        person_group_id=person_group_id,
        target_user_id=target_user_id,
        source_thumbnail_path=source["thumbnailPath"],
    )

    # The composite PK makes this race-safe without an advisory lock: a
    # concurrent transaction inserting the same (ownerId, personGroupId) loses
    # the conflict and we keep whichever row landed first.
    await conn.execute(
        """
        INSERT INTO person ("ownerId", "personGroupId", name, "thumbnailPath",
                            "isHidden", "birthDate", "isFavorite", color)
        VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)
        ON CONFLICT ("ownerId", "personGroupId") DO NOTHING
        """,
        target_user_id,
        person_group_id,
        source["name"],
        target_thumbnail,
        source["isHidden"],
        source["birthDate"],
        source["color"],
    )

    logger.info(
        "Created person for user %s in group %s (name=%r)",
        target_user_id, person_group_id, source["name"],
    )
    return person_group_id


async def sync_person_names(conn: asyncpg.Connection) -> int:
    """Copy source person names onto target persons that have no name yet.

    Only fills empty names. Names are per-user in v3.2.0 by design — if the
    target user has named someone themselves, that is their choice and the
    sidecar must not stomp it.
    """
    updated = await conn.fetch(
        """
        UPDATE person t
        SET name = s.name
        FROM person s, _face_sync_asset_map m
        WHERE s."personGroupId" = t."personGroupId"
          AND s."ownerId" = m.source_user_id
          AND t."ownerId" = m.target_user_id
          AND s.name <> ''
          AND (t.name = '' OR t.name IS NULL)
        RETURNING t."ownerId", t."personGroupId", t.name
        """,
    )

    for row in updated:
        logger.info(
            "Named person group %s for user %s: %r",
            row["personGroupId"], row["ownerId"], row["name"],
        )

    return len(updated)


async def sync_person_visibility(conn: asyncpg.Connection) -> int:
    """Copy source ``isHidden`` onto target persons in the same group."""
    updated = await conn.fetch(
        """
        UPDATE person t
        SET "isHidden" = s."isHidden"
        FROM person s, _face_sync_asset_map m
        WHERE s."personGroupId" = t."personGroupId"
          AND s."ownerId" = m.source_user_id
          AND t."ownerId" = m.target_user_id
          AND t."isHidden" IS DISTINCT FROM s."isHidden"
        RETURNING t."personGroupId"
        """,
    )
    return len(updated)


async def sync_person_thumbnails(conn: asyncpg.Connection) -> int:
    """Hardlink thumbnails for target persons that still have none."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT t."ownerId" AS target_user_id,
               t."personGroupId" AS person_group_id,
               s."thumbnailPath" AS source_thumb
        FROM person t
        JOIN _face_sync_asset_map m ON m.target_user_id = t."ownerId"
        JOIN person s ON s."personGroupId" = t."personGroupId"
            AND s."ownerId" = m.source_user_id
        WHERE s."thumbnailPath" <> ''
          AND (t."thumbnailPath" = '' OR t."thumbnailPath" IS NULL)
        """,
    )

    count = 0
    for row in rows:
        target_thumb = _hardlink_person_thumbnail(
            person_group_id=row["person_group_id"],
            target_user_id=row["target_user_id"],
            source_thumbnail_path=row["source_thumb"],
        )
        if target_thumb:
            await conn.execute(
                'UPDATE person SET "thumbnailPath" = $1 '
                'WHERE "ownerId" = $2 AND "personGroupId" = $3',
                target_thumb, row["target_user_id"], row["person_group_id"],
            )
            count += 1

    return count


async def cleanup_orphaned_persons(conn: asyncpg.Connection) -> int:
    """Remove sidecar-created person rows that no longer have any faces.

    Safety: only delete a person row with NO remaining faces in its group owned
    by that user. Immich sets ``asset_face."personGroupId"`` to NULL when the
    last person row in a group goes (``deleteEmptyGroups``), so deleting a row
    whose faces survive would silently unassign them.

    Scope: restricted to users who belong to a cluster group. A person row can
    only be created by ``ensure_target_person`` (or Immich itself) for a user
    participating in cluster-group identity sharing, so this keeps the sweep
    away from persons belonging to accounts the sidecar has no business
    touching. Deliberately not scoped through ``_face_sync_asset_map``: an
    empty target person row can exist before any asset has ever synced for
    that pair (e.g. Phase 3 ran ahead of Phase 1 finding new assets), and the
    map only gains a row once an asset does.
    """
    deleted = await conn.fetch(
        """
        DELETE FROM person t
        USING "user" u
        WHERE t."ownerId" = u.id
          AND u."clusterGroupId" IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM asset_face af
              JOIN asset a ON a.id = af."assetId"
              WHERE af."personGroupId" = t."personGroupId"
                AND a."ownerId" = t."ownerId"
                AND af."deletedAt" IS NULL
          )
        RETURNING t."personGroupId"
        """,
    )

    if deleted:
        logger.info("Cleaned up %d orphaned target persons", len(deleted))

    return len(deleted)
