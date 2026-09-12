import logging
import os
from pathlib import Path
from uuid import UUID

import asyncpg

from src.config import settings
from src.file_ops import validate_path_within_upload

logger = logging.getLogger(__name__)

# The scope shared by every cross-user write below: a person group the sidecar
# actually put on this pair's synced assets.
#
# Both halves are load-bearing. The first bounds the write to managed user
# pairs. The second is the one that is easy to talk yourself out of: cluster
# groups make Immich file BOTH users' faces into the same person_group by
# construction, so "same group + mapped pair" also matches people the target
# found entirely on their own photos. Without it the source's metadata lands
# on strangers.
#
# Written as semi-joins, not joins. _face_sync_asset_map holds one row per
# synced asset, so joining it directly fans every person row out across the
# whole library. Interpolated rather than shared as a view because these run
# against Immich's database, which the sidecar does not own the schema of.
_SYNCED_GROUP_SCOPE = """
          AND EXISTS (
              SELECT 1 FROM _face_sync_asset_map m
              WHERE m.source_user_id = s."ownerId"
                AND m.target_user_id = t."ownerId"
          )
          AND EXISTS (
              SELECT 1 FROM _face_sync_asset_map m2
              JOIN asset_face af ON af."assetId" = m2.target_asset_id
              WHERE m2.target_user_id = t."ownerId"
                AND af."personGroupId" = t."personGroupId"
                AND af."deletedAt" IS NULL
          )
"""


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
    created = await conn.fetch(
        """
        INSERT INTO person ("ownerId", "personGroupId", name, "thumbnailPath",
                            "isHidden", "birthDate", "isFavorite", color)
        VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)
        ON CONFLICT ("ownerId", "personGroupId") DO NOTHING
        RETURNING "personGroupId"
        """,
        target_user_id,
        person_group_id,
        source["name"],
        target_thumbnail,
        source["isHidden"],
        source["birthDate"],
        source["color"],
    )

    # RETURNING yields nothing when the ON CONFLICT swallowed the insert, so
    # this says "created" only when a row was. Logging it unconditionally
    # reported a creation that did not happen on a lost race -- the kind of
    # line someone reads during an incident and believes.
    if created:
        logger.info(
            "Created person for user %s in group %s (name=%r)",
            target_user_id, person_group_id, source["name"],
        )
    elif target_thumbnail:
        # We hardlinked a thumbnail for a row we did not end up writing. The
        # file is harmless (the source holds the inode) but nothing references
        # it, so say so rather than leaving it silently on disk.
        logger.debug(
            "Lost the insert race for group %s; unreferenced thumbnail at %s",
            person_group_id, target_thumbnail,
        )
    return person_group_id


async def sync_person_names(conn: asyncpg.Connection) -> int:
    """Copy source person names onto target persons that have no name yet.

    Only fills empty names. Names are per-user in v3.2.0 by design — if the
    target user has named someone themselves, that is their choice and the
    sidecar must not stomp it.

    The fill-only rule bounds the damage but not the reach, so this carries
    ``_SYNCED_GROUP_SCOPE`` like the other cross-user writes.
    """
    updated = await conn.fetch(
        """
        UPDATE person t
        SET name = s.name
        FROM person s
        WHERE s."personGroupId" = t."personGroupId"
          AND s.name <> ''
          AND t.name = ''
        """
        + _SYNCED_GROUP_SCOPE
        + """
        RETURNING t."ownerId", t."personGroupId", t.name
        """,
    )

    for row in updated:
        logger.info(
            "Named person group %s for user %s: %r",
            row["personGroupId"], row["ownerId"], row["name"],
        )

    return len(updated)




async def sync_person_thumbnails(conn: asyncpg.Connection) -> int:
    """Hardlink thumbnails for target persons that still have none.

    Carries ``_SYNCED_GROUP_SCOPE`` like the other cross-user writes. It is
    fill-only on ``thumbnailPath = ''``, but it creates files on disk, so the
    reach is worth bounding.
    """
    # DISTINCT ON, not DISTINCT: with two jobs into one target, the same
    # (target, group) appears once per source, and those sources can hold
    # different thumbnailPath values. Plain DISTINCT keeps both rows, so the
    # file gets linked once, the UPDATE runs twice and the count over-reports.
    # Picking the lowest source ownerId is arbitrary but stated, which beats
    # arbitrary and incidental.
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (t."ownerId", t."personGroupId")
               t."ownerId" AS target_user_id,
               t."personGroupId" AS person_group_id,
               s."thumbnailPath" AS source_thumb
        FROM person t
        JOIN person s ON s."personGroupId" = t."personGroupId"
        WHERE s."thumbnailPath" <> ''
          AND t."thumbnailPath" = ''
        """
        + _SYNCED_GROUP_SCOPE
        + """
        ORDER BY t."ownerId", t."personGroupId", s."ownerId"
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
    """Remove unnamed target person rows that no longer have any faces.

    This is what lets an emptied person group be collected at all: Immich's
    ``deleteEmptyGroups`` drops groups with no *person* rows, so while the
    sidecar's row sits there the group survives as a person with no photos.

    Safety: only delete a person row when its group has NO remaining faces at
    all, owned by anyone. Immich sets ``asset_face."personGroupId"`` to NULL
    when the last person row in a group goes (``deleteEmptyGroups``), and it
    does so for *every* face in the group, not just the deleting user's. This
    is the only statement in the sync cycle that can empty a group, so it is
    the only path that could write into the source user's own library — hence
    the face guard is group-scoped, not owner-scoped. Anything narrower risks
    silently unassigning the source user's faces on their own photos.

    That includes soft-deleted faces, which is why this guard alone does not
    filter on ``deletedAt``. A trashed face still carries its
    ``"personGroupId"``, and the FK is ON DELETE SET NULL, so emptying the
    group nulls it exactly like a live one — except the source user cannot see
    it happen and restoring from trash will not bring the assignment back. The
    question this guard asks is "does any row still reference this group", and
    a soft-deleted row does. The ``deletedAt IS NULL`` filters elsewhere in
    this module are inclusion scopes ("which groups are worth syncing"), where
    ignoring trashed faces is correct.

    Scope: restricted to target users the sidecar actually manages, via
    ``_face_sync_asset_map``, plus a ``NOT EXISTS`` guard that *no* mapped
    source for that target still has a person row for the group. The negation
    must wrap the whole map lookup: nested inside the ``EXISTS`` it would be
    satisfied by any single mapped source lacking a row, which with two jobs
    sharing a target makes every group deletion-eligible. Compare the
    mirror-image shape in ``delete_synced.py`` / ``reset.sh``, which require a
    source row to *exist*. Without this scoping the sweep would touch every
    account in the database — this table is the only durable record of "the
    sidecar created assets/persons for this pair."

    Note: a target person row can exist for one cycle before the map gains a
    row for that pair (e.g. if person-metadata sync ever ran ahead of the
    asset sync that produces the first mapped asset). That row is simply
    skipped by this sweep until the map catches up — harmless and
    self-correcting, unlike widening the DELETE's scope to compensate.

    Names are the one piece of provenance left. Nothing here proves the
    sidecar *created* the row it is about to delete — ``_face_sync_person_map``
    carried that and v3.2.0 retired it — and the three guards above are all
    satisfied by a person the target user found on their own photos and then
    deleted the photos of. So a named row is never touched, on the same
    principle prune_inflated_people.py uses: a name means a human decided this
    person was real. ``ensure_target_person`` creates rows with an empty name,
    and sync_person_names only fills one when the group still carries a synced
    face, which this sweep requires to be gone. The cost is that a named
    person can outlive its last face as an empty entry in the target's people
    list, which the target user can delete themselves.
    """
    deleted = await conn.fetch(
        """
        DELETE FROM person t
        WHERE t.name = ''
          AND EXISTS (
              SELECT 1 FROM _face_sync_asset_map m
              WHERE m.target_user_id = t."ownerId"
          )
          AND NOT EXISTS (
              SELECT 1 FROM _face_sync_asset_map m
              JOIN person s ON s."personGroupId" = t."personGroupId"
                           AND s."ownerId" = m.source_user_id
              WHERE m.target_user_id = t."ownerId"
          )
          AND NOT EXISTS (
              SELECT 1 FROM asset_face af
              WHERE af."personGroupId" = t."personGroupId"
          )
        RETURNING t."ownerId", t."personGroupId"
        """,
    )

    if deleted:
        logger.info("Cleaned up %d orphaned target persons", len(deleted))

    return len(deleted)


async def delete_target_person_in_shared_group(
    conn: asyncpg.Connection, target_user_id: UUID, person_group_id: UUID,
) -> bool:
    """Teardown: drop one target person row from a group a source still holds.

    For the teardown tools (``delete_synced.py``, ``reset.sh``), not the sync
    cycle. Returns True if a row was actually deleted.

    This is the mirror image of ``cleanup_orphaned_persons``, and the two
    guards are deliberately not the same shape — the difference is worth
    stating, because it reads like drift and is not:

    * The ``EXISTS`` requires a *mapped source* to still hold a person row on
      the group. That, not the face guard, is what makes this safe: the group
      keeps a row, so Immich's ``deleteEmptyGroups`` cannot drop it and null
      every face in it. ``cleanup_orphaned_persons`` requires the opposite
      (``NOT EXISTS``) because it runs when the sidecar's persons are the last
      ones left, and so it needs the group-scoped face guard instead.
    * The face guard here is therefore free to be *owner-scoped*: "is the
      target still using this person?". Group-scoped, as in
      ``cleanup_orphaned_persons``, it would match the source's faces on the
      source's own photos — the normal state during teardown — and refuse
      every deletion, making both tools silent no-ops.

    One statement, so both conditions are evaluated at delete time.
    ``delete_synced.py`` lists its candidates before an interactive prompt,
    which leaves a human-scale window in which the source's person row could
    go; re-checking here closes it.
    """
    deleted = await conn.fetchval(
        """
        DELETE FROM person t
        WHERE t."ownerId" = $1
          AND t."personGroupId" = $2
          AND EXISTS (
              SELECT 1 FROM _face_sync_asset_map m
              JOIN person s ON s."personGroupId" = t."personGroupId"
                           AND s."ownerId" = m.source_user_id
              WHERE m.target_user_id = t."ownerId"
          )
          AND NOT EXISTS (
              SELECT 1 FROM asset_face af
              JOIN asset a ON a.id = af."assetId"
              WHERE af."personGroupId" = t."personGroupId"
                AND a."ownerId" = t."ownerId"
                AND af."deletedAt" IS NULL
          )
        RETURNING t."personGroupId"
        """,
        target_user_id,
        person_group_id,
    )
    return deleted is not None
