"""Immich schema validation.

Validates that the Immich database schema contains all tables and columns
this sidecar depends on. Run on startup and before each sync cycle to detect
breaking schema changes from Immich upgrades.
"""

import logging

import asyncpg

from src.db import acquire

logger = logging.getLogger(__name__)

REQUIRED_SCHEMA: dict[str, set[str]] = {
    "asset": {
        "id", "deviceAssetId", "ownerId", "deviceId", "type", "originalPath",
        "fileCreatedAt", "fileModifiedAt", "isFavorite", "duration",
        "encodedVideoPath", "checksum", "livePhotoVideoId", "originalFileName",
        "thumbhash", "isOffline", "libraryId", "isExternal", "localDateTime",
        "stackId", "duplicateId", "status", "visibility", "width", "height",
        "isEdited", "deletedAt",
    },
    "asset_exif": {
        "assetId", "dateTimeOriginal",
    },
    "asset_file": {
        "id", "assetId", "type", "path", "isEdited", "isProgressive",
    },
    "asset_job_status": {
        "assetId", "facesRecognizedAt", "metadataExtractedAt",
        "duplicatesDetectedAt", "ocrAt",
    },
    "smart_search": {
        "assetId", "embedding",
    },
    "asset_face": {
        "id", "assetId", "personId", "imageWidth", "imageHeight",
        "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
        "sourceType", "deletedAt", "updatedAt", "isVisible",
    },
    "face_search": {
        "faceId", "embedding",
    },
    "person": {
        "id", "ownerId", "name", "thumbnailPath", "isHidden", "birthDate",
        "faceAssetId", "isFavorite", "color",
    },
    "album": {
        "id", "ownerId", "updatedAt", "deletedAt",
    },
    "album_asset": {
        "albumId", "assetId",
    },
    "library": {
        "id", "ownerId", "deletedAt",
    },
    "user": {
        "id", "deletedAt",
    },
}


class SchemaValidationError(RuntimeError):
    """Raised when the Immich database schema doesn't match expectations."""


async def validate_schema(conn: asyncpg.Connection | None = None) -> None:
    """Validate that all required Immich tables and columns exist.

    Queries information_schema.columns and compares against REQUIRED_SCHEMA.
    Raises SchemaValidationError listing every missing table and/or column.
    """
    expected_tables = list(REQUIRED_SCHEMA.keys())

    async def _check(c: asyncpg.Connection) -> None:
        rows = await c.fetch(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY($1)
            """,
            expected_tables,
        )

        # Build actual schema: table -> set of columns
        actual: dict[str, set[str]] = {}
        for row in rows:
            actual.setdefault(row["table_name"], set()).add(row["column_name"])

        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}

        for table, expected_cols in REQUIRED_SCHEMA.items():
            if table not in actual:
                missing_tables.append(table)
                continue
            missing = expected_cols - actual[table]
            if missing:
                missing_columns[table] = sorted(missing)

        if missing_tables or missing_columns:
            parts = []
            if missing_tables:
                parts.append(f"Missing tables: {', '.join(sorted(missing_tables))}")
            for table, cols in sorted(missing_columns.items()):
                parts.append(f"Missing columns in '{table}': {', '.join(cols)}")
            msg = (
                "Immich schema validation failed — the database schema does not match "
                "what this sidecar expects. This usually means Immich was upgraded to a "
                "version with breaking schema changes.\n" + "\n".join(parts)
            )
            raise SchemaValidationError(msg)

        logger.info("Schema validation passed (%d tables, %d columns checked)",
                     len(REQUIRED_SCHEMA),
                     sum(len(cols) for cols in REQUIRED_SCHEMA.values()))

    if conn is not None:
        await _check(conn)
    else:
        async with acquire() as c:
            await _check(c)
