import logging
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import asyncpg

from src.config import SyncJob
from src.file_ops import hardlink_asset_files, remove_hardlinks

logger = logging.getLogger(__name__)


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
          )
          AND NOT EXISTS (
            SELECT 1 FROM _face_sync_skipped s
            WHERE s.source_asset_id = a.id
          )
        LIMIT $3
        """,
        job.source_user_id,
        job.source_path_prefix,
        limit,
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


async def record_skipped_duplicates(conn: asyncpg.Connection, source_asset_ids: set[UUID]) -> None:
    """Batch-insert skip records for duplicate assets."""
    if not source_asset_ids:
        return
    await conn.execute(
        """
        INSERT INTO _face_sync_skipped (source_asset_id, reason)
        SELECT unnest($1::uuid[]), 'duplicate_filename'
        ON CONFLICT (source_asset_id) DO NOTHING
        """,
        list(source_asset_ids),
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
        result = await conn.execute(
            """
            INSERT INTO _face_sync_asset_map (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (source_asset_id) DO NOTHING
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
                id, "deviceAssetId", "ownerId", "deviceId", type, "originalPath",
                "fileCreatedAt", "fileModifiedAt", "isFavorite", duration,
                "encodedVideoPath", checksum, "livePhotoVideoId", "originalFileName",
                thumbhash, "isOffline", "libraryId", "isExternal", "localDateTime",
                "stackId", "duplicateId", status, visibility, width, height, "isEdited"
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                $7, $8, $9, $10,
                $11, $12, $13, $14,
                $15, $16, $17, $18, $19,
                $20, $21, $22, $23, $24, $25, $26
            )
            """,
            target_id,
            source["deviceAssetId"],
            target_user_id,
            source["deviceId"],
            source["type"],
            target_path,
            source["fileCreatedAt"],
            source["fileModifiedAt"],
            False,  # isFavorite — don't copy preference
            source["duration"],
            source["encodedVideoPath"],
            source["checksum"],
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
            source["isEdited"],
        )

        # 2. Copy exif
        await _copy_exif(conn, source_id, target_id)

        # 3. Hardlink thumbnails/previews and create asset_files records
        created_files = await _sync_asset_files(conn, source_id, target_id, source["ownerId"], target_user_id)

        # 4. Set job status to mark as fully processed
        await conn.execute(
            """
            INSERT INTO asset_job_status ("assetId", "facesRecognizedAt", "metadataExtractedAt", "duplicatesDetectedAt", "ocrAt")
            VALUES ($1, $2, $2, $2, $2)
            """,
            target_id,
            now,
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
            INSERT INTO _face_sync_skipped (source_asset_id, reason)
            VALUES ($1, 'duplicate_checksum')
            ON CONFLICT (source_asset_id) DO NOTHING
            """,
            source_id,
        )
        logger.warning("Skipping asset %s: duplicate checksum for target user", source_id)
        return None
    except Exception:
        await conn.execute("ROLLBACK TO SAVEPOINT sync_asset")
        remove_hardlinks(created_files)
        logger.exception("Failed to sync asset %s", source_id)
        return None


async def _copy_exif(conn: asyncpg.Connection, source_id: UUID, target_id: UUID) -> None:
    """Copy exif data from source asset to target asset."""
    exif = await conn.fetchrow('SELECT * FROM asset_exif WHERE "assetId" = $1', source_id)
    if exif is None:
        return

    # Hardcoded allowlist of EXIF columns to copy (excludes assetId, updatedAt, updateId)
    cols = [
        "make", "model", "exifImageWidth", "exifImageHeight", "fileSizeInByte",
        "orientation", "dateTimeOriginal", "modifyDate", "lensModel", "fNumber",
        "focalLength", "iso", "latitude", "longitude", "city", "state", "country",
        "description", "fps", "exposureTime", "livePhotoCID", "timeZone",
        "projectionType", "profileDescription", "colorspace", "bitsPerSample",
        "autoStackId", "rating", "tags", "lockedProperties",
    ]
    # Filter to only columns present in this row (forward-compatible if Immich removes a column)
    cols = [c for c in cols if c in exif]
    col_names = ', '.join(f'"{c}"' for c in cols)
    placeholders = ', '.join(f'${i + 2}' for i in range(len(cols)))

    await conn.execute(
        f'INSERT INTO asset_exif ("assetId", {col_names}) VALUES ($1, {placeholders})',
        target_id,
        *[exif[c] for c in cols],
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
