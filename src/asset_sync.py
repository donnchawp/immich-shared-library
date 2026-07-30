import logging
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import asyncpg

from src.config import SyncJob
from src.file_ops import hardlink_asset_files, remove_hardlinks

logger = logging.getLogger(__name__)

# Video stream tables added in Immich v3. All are PK'd on assetId and CASCADE
# from asset. Copied so Immich doesn't need to re-probe synced video assets.
_VIDEO_STREAM_TABLES = [
    ("asset_video",
     'bitrate, "frameCount", "timeBase", "index", profile, level, '
     '"colorPrimaries", "colorTransfer", "colorMatrix", "dvProfile", '
     '"dvLevel", "dvBlSignalCompatibilityId", "codecName", "formatName", '
     '"formatLongName", "pixelFormat"'),
    ("asset_audio", 'bitrate, "index", profile, "codecName"'),
    ("asset_keyframe",
     'pts, "accDuration", "ownDuration", "totalDuration", "packetCount", '
     '"outputFrames"'),
]


def _remap_asset_path(source_path: str, job: SyncJob) -> str:
    """Remap an asset's originalPath from the source prefix to the target prefix.

    e.g., /external_library/donncha/photo.jpg -> /external_library/jacinta/photo.jpg
    Normalizes the result to collapse any '..' components.
    """
    if job.target_path_prefix and job.source_path_prefix:
        if source_path.startswith(job.source_path_prefix):
            remapped = job.target_path_prefix + source_path[len(job.source_path_prefix):]
            normalized = os.path.normpath(remapped)
            if not normalized.startswith(job.target_path_prefix):
                raise ValueError(
                    f"Remapped path {remapped} normalizes to {normalized}, "
                    f"which escapes target prefix {job.target_path_prefix}"
                )
            return normalized
    return os.path.normpath(source_path)


async def get_unsynced_source_assets(
    conn: asyncpg.Connection, job: SyncJob, limit: int = 500
) -> list[asyncpg.Record]:
    """Find fully-processed source assets in the shared directory that haven't been synced yet.

    Returns up to `limit` assets per call to avoid loading an entire library into memory.
    """
    return await conn.fetch(
        """
        SELECT a.*
        FROM asset a
        JOIN asset_job_status ajs ON ajs."assetId" = a.id
        JOIN smart_search ss ON ss."assetId" = a.id
        WHERE a."ownerId" = $1
          AND starts_with(a."originalPath", $2)
          AND a."deletedAt" IS NULL
          AND ajs."metadataExtractedAt" IS NOT NULL
          AND ajs."facesRecognizedAt" IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM _face_sync_asset_map m
            WHERE m.source_asset_id = a.id
              AND m.target_user_id = $4
          )
          AND NOT EXISTS (
            SELECT 1 FROM _face_sync_skipped s
            WHERE s.source_asset_id = a.id
              AND s.target_user_id = $4
          )
        LIMIT $3
        """,
        job.source_user_id,
        job.source_path_prefix,
        limit,
        job.target_user_id,
    )


async def find_duplicate_filenames(
    conn: asyncpg.Connection, source_assets: list[asyncpg.Record], job: SyncJob
) -> set[UUID]:
    """Find source assets that already exist in the target user's own uploads by filename stem + capture time.

    Matches on filename stem (without extension) and EXIF dateTimeOriginal.
    Only checks the target user's non-sync-library assets (their own uploads).
    Returns set of source asset IDs that are duplicates.
    """
    if not source_assets:
        return set()

    # Build stem -> list of source asset IDs mapping
    stem_to_sources: dict[str, list[UUID]] = {}
    source_ids = []
    for sa in source_assets:
        stem = PurePosixPath(sa["originalFileName"]).stem
        stem_to_sources.setdefault(stem, []).append(sa["id"])
        source_ids.append(sa["id"])

    # Batch-fetch source EXIF dateTimeOriginal
    source_exif_rows = await conn.fetch(
        """
        SELECT "assetId", "dateTimeOriginal"
        FROM asset_exif
        WHERE "assetId" = ANY($1::uuid[])
          AND "dateTimeOriginal" IS NOT NULL
        """,
        source_ids,
    )
    source_exif: dict[UUID, datetime] = {
        row["assetId"]: row["dateTimeOriginal"] for row in source_exif_rows
    }

    # Only check stems where at least one source asset has EXIF dateTimeOriginal
    stems_to_check = []
    for stem, sids in stem_to_sources.items():
        if any(sid in source_exif for sid in sids):
            stems_to_check.append(stem)

    if not stems_to_check:
        return set()

    # Find target user's own uploads matching any of those stems
    target_matches = await conn.fetch(
        """
        SELECT regexp_replace(a."originalFileName", '\\.[^.]+$', '') AS stem,
               e."dateTimeOriginal"
        FROM asset a
        JOIN asset_exif e ON e."assetId" = a.id
        WHERE a."ownerId" = $1
          AND a."libraryId" IS DISTINCT FROM $2
          AND a."deletedAt" IS NULL
          AND e."dateTimeOriginal" IS NOT NULL
          AND regexp_replace(a."originalFileName", '\\.[^.]+$', '') = ANY($3::text[])
        """,
        job.target_user_id,
        job.target_library_id,
        stems_to_check,
    )

    # Build index of (stem, dateTimeOriginal) that exist in target
    target_index: set[tuple[str, datetime]] = {
        (row["stem"], row["dateTimeOriginal"]) for row in target_matches
    }

    if not target_index:
        return set()

    # Match source assets against target index
    duplicates: set[UUID] = set()
    for sa in source_assets:
        sid = sa["id"]
        if sid not in source_exif:
            continue  # No EXIF dateTimeOriginal — can't confirm, don't skip
        stem = PurePosixPath(sa["originalFileName"]).stem
        if (stem, source_exif[sid]) in target_index:
            duplicates.add(sid)

    return duplicates


async def record_skipped_duplicates(
    conn: asyncpg.Connection, source_asset_ids: set[UUID], target_user_id: UUID
) -> None:
    """Batch-insert skip records for duplicate assets."""
    if not source_asset_ids:
        return
    await conn.execute(
        """
        INSERT INTO _face_sync_skipped (source_asset_id, target_user_id, reason)
        SELECT unnest($1::uuid[]), $2, 'duplicate_filename'
        ON CONFLICT (source_asset_id, target_user_id) DO NOTHING
        """,
        list(source_asset_ids),
        target_user_id,
    )


async def sync_asset(conn: asyncpg.Connection, source: asyncpg.Record, job: SyncJob) -> UUID | None:
    """Create a complete asset record for the target user from a source asset.

    Uses a savepoint so a single asset failure doesn't roll back the entire batch.
    Returns the new target asset ID, or None on failure.
    """
    source_id = source["id"]
    target_id = uuid4()
    now = datetime.now(timezone.utc)
    target_user_id = job.target_user_id
    target_library_id = job.target_library_id

    target_path = _remap_asset_path(source["originalPath"], job)

    # Idempotency: check if a target asset already exists for this path + owner + library
    existing = await conn.fetchval(
        """
        SELECT id FROM asset
        WHERE "ownerId" = $1 AND "libraryId" = $2 AND "originalPath" = $3 AND "deletedAt" IS NULL
        """,
        target_user_id,
        target_library_id,
        target_path,
    )
    if existing is not None:
        # Already synced but mapping was lost (crash recovery) — re-create mapping
        # Use conflict on the composite key (source_asset_id, target_user_id) since
        # that's our unique constraint. Also catches target_asset_id conflicts.
        result = await conn.execute(
            """
            INSERT INTO _face_sync_asset_map (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (source_asset_id, target_user_id) DO NOTHING
            """,
            source_id, existing, job.source_user_id, target_user_id, now,
        )
        if result == "INSERT 0 1":
            logger.info("Recovered mapping for existing asset %s -> %s", source_id, existing)
        return existing

    # Use a savepoint so failure rolls back only this asset, not the whole transaction
    await conn.execute("SAVEPOINT sync_asset")
    created_files: list[str] = []
    try:
        # 1. Insert asset record
        await conn.execute(
            """
            INSERT INTO asset (
                id, "ownerId", type, "originalPath",
                "fileCreatedAt", "fileModifiedAt", "isFavorite", duration,
                checksum, "checksumAlgorithm", "livePhotoVideoId", "originalFileName",
                thumbhash, "isOffline", "libraryId", "isExternal", "localDateTime",
                "stackId", "duplicateId", status, visibility, width, height
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, $10, $11, $12,
                $13, $14, $15, $16, $17,
                $18, $19, $20, $21, $22, $23
            )
            """,
            target_id,
            target_user_id,
            source["type"],
            target_path,
            source["fileCreatedAt"],
            source["fileModifiedAt"],
            False,  # isFavorite — don't copy preference
            source["duration"],
            source["checksum"],
            source["checksumAlgorithm"],
            None,  # livePhotoVideoId — handle separately if needed
            source["originalFileName"],
            source["thumbhash"],
            source["isOffline"],
            target_library_id,
            True,  # isExternal
            source["localDateTime"],
            None,  # stackId
            None,  # duplicateId
            source["status"],
            source["visibility"],
            source["width"],
            source["height"],
            # isEdited is omitted deliberately: it's a derived flag that the
            # asset_edit_insert/asset_edit_delete triggers maintain. It defaults
            # to false here and step 5d flips it by copying the edit rows.
        )

        # 2. Copy exif
        await _copy_exif(conn, source_id, target_id)

        # 3. Hardlink thumbnails/previews and create asset_files records
        created_files = await _sync_asset_files(conn, source_id, target_id, source["ownerId"], target_user_id)

        # 4. Set job status to mark as fully processed.
        # ocrAt comes from the source row: if the source hasn't been OCR'd
        # (e.g. OCR disabled), it stays NULL so Immich can OCR the target itself.
        await conn.execute(
            """
            INSERT INTO asset_job_status ("assetId", "facesRecognizedAt", "metadataExtractedAt", "duplicatesDetectedAt", "ocrAt")
            SELECT $1, $2, $2, $2, src."ocrAt"
            FROM asset_job_status src
            WHERE src."assetId" = $3
            """,
            target_id,
            now,
            source_id,
        )

        # 5. Copy smart_search embedding
        await conn.execute(
            """
            INSERT INTO smart_search ("assetId", embedding)
            SELECT $1, embedding
            FROM smart_search
            WHERE "assetId" = $2
            """,
            target_id,
            source_id,
        )

        # 5b. Copy OCR results (detected text boxes + search text), same
        # pattern as smart_search. asset_ocr.id and updateId have DB defaults.
        await conn.execute(
            """
            INSERT INTO asset_ocr ("assetId", x1, y1, x2, y2, x3, y3, x4, y4,
                                   "boxScore", "textScore", text, "isVisible")
            SELECT $1, x1, y1, x2, y2, x3, y3, x4, y4,
                   "boxScore", "textScore", text, "isVisible"
            FROM asset_ocr
            WHERE "assetId" = $2
            """,
            target_id,
            source_id,
        )
        await conn.execute(
            """
            INSERT INTO ocr_search ("assetId", text)
            SELECT $1, text
            FROM ocr_search
            WHERE "assetId" = $2
            """,
            target_id,
            source_id,
        )

        # 5c. Copy video/audio stream metadata and keyframe index.
        # The INSERT...SELECT is a no-op for photos (no source rows).
        for table, columns in _VIDEO_STREAM_TABLES:
            await conn.execute(
                f'INSERT INTO {table} ("assetId", {columns}) '
                f'SELECT $1, {columns} FROM {table} WHERE "assetId" = $2',
                target_id,
                source_id,
            )

        # 5d. Copy edit history (crop/rotate/mirror). The hardlinked thumbnails
        # are the source's *edited* renders, so the target must claim the same
        # edits to stay consistent. Inserting these fires asset_edit_insert,
        # which sets asset."isEdited" = true — that's why step 1 doesn't supply
        # it. parameters is pure geometry (no asset ids or paths), so it copies
        # verbatim. id/updatedAt/updateId have DB defaults. Copy-once, like
        # exif and OCR: later source edits won't propagate.
        await conn.execute(
            """
            INSERT INTO asset_edit ("assetId", action, parameters, sequence)
            SELECT $1, action, parameters, sequence
            FROM asset_edit
            WHERE "assetId" = $2
            """,
            target_id,
            source_id,
        )

        # 6. Track the mapping
        await conn.execute(
            """
            INSERT INTO _face_sync_asset_map (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            source_id,
            target_id,
            job.source_user_id,
            target_user_id,
            now,
        )

        await conn.execute("RELEASE SAVEPOINT sync_asset")
        logger.info("Synced asset %s -> %s (%s)", source_id, target_id, source["originalFileName"])
        return target_id

    except asyncpg.UniqueViolationError:
        await conn.execute("ROLLBACK TO SAVEPOINT sync_asset")
        remove_hardlinks(created_files)
        await conn.execute(
            """
            INSERT INTO _face_sync_skipped (source_asset_id, target_user_id, reason)
            VALUES ($1, $2, 'duplicate_checksum')
            ON CONFLICT (source_asset_id, target_user_id) DO NOTHING
            """,
            source_id,
            target_user_id,
        )
        logger.warning("Skipping asset %s: duplicate checksum for target user", source_id)
        return None
    except Exception:
        await conn.execute("ROLLBACK TO SAVEPOINT sync_asset")
        remove_hardlinks(created_files)
        logger.exception("Failed to sync asset %s", source_id)
        return None


_EXIF_COLUMNS = frozenset([
    "make", "model", "exifImageWidth", "exifImageHeight", "fileSizeInByte",
    "orientation", "dateTimeOriginal", "modifyDate", "lensModel", "fNumber",
    "focalLength", "iso", "latitude", "longitude", "city", "state", "country",
    "description", "fps", "exposureTime", "livePhotoCID", "timeZone",
    "projectionType", "profileDescription", "colorspace", "bitsPerSample",
    "autoStackId", "rating", "tags", "lockedProperties",
])

_exif_warned: set[str] = set()


async def _copy_exif(conn: asyncpg.Connection, source_id: UUID, target_id: UUID) -> None:
    """Copy exif data from source asset to target asset."""
    exif = await conn.fetchrow('SELECT * FROM asset_exif WHERE "assetId" = $1', source_id)
    if exif is None:
        return

    # Warn once per missing allowlisted column (likely renamed in an Immich upgrade)
    actual_cols = set(exif.keys())
    missing = _EXIF_COLUMNS - actual_cols - _exif_warned
    if missing:
        _exif_warned.update(missing)
        logger.warning("EXIF columns in allowlist but missing from DB (renamed?): %s", ", ".join(sorted(missing)))

    # Filter to only allowlisted columns present in this row
    cols = [c for c in exif.keys() if c in _EXIF_COLUMNS]
    values = [target_id] + [exif[c] for c in cols]

    # Column names are from _EXIF_COLUMNS (a code constant, not user input)
    col_names = ', '.join(f'"{c}"' for c in cols)
    placeholders = ', '.join(f'${i + 2}' for i in range(len(cols)))

    await conn.execute(
        f'INSERT INTO asset_exif ("assetId", {col_names}) VALUES ($1, {placeholders})',
        *values,
    )


async def _sync_asset_files(
    conn: asyncpg.Connection,
    source_id: UUID,
    target_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
) -> list[str]:
    """Hardlink source asset files and create records for target asset.

    Returns the list of created target file paths (for rollback cleanup).
    """
    files = await conn.fetch(
        'SELECT id, "assetId", type, path, "isEdited", "isProgressive" FROM asset_file WHERE "assetId" = $1',
        source_id,
    )
    if not files:
        return []

    source_files = [
        {"type": f["type"], "path": f["path"], "is_edited": f["isEdited"], "is_progressive": f["isProgressive"]}
        for f in files
    ]

    new_files = hardlink_asset_files(
        source_user_id=source_user_id,
        target_user_id=target_user_id,
        source_asset_id=source_id,
        target_asset_id=target_id,
        source_files=source_files,
    )

    for nf in new_files:
        await conn.execute(
            """
            INSERT INTO asset_file (id, "assetId", type, path, "isEdited", "isProgressive")
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            uuid4(),
            target_id,
            nf["type"],
            nf["path"],
            nf["is_edited"],
            nf["is_progressive"],
        )

    return [nf["path"] for nf in new_files]
