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


# Columns the sidecar supplies when INSERTing into each Immich table.
# Used to detect new NOT NULL columns (without defaults) that would cause
# INSERT failures after an Immich upgrade.
INSERTED_COLUMNS: dict[str, set[str]] = {
    "asset": {
        "id", "deviceAssetId", "ownerId", "deviceId", "type", "originalPath",
        "fileCreatedAt", "fileModifiedAt", "isFavorite", "duration",
        "encodedVideoPath", "checksum", "livePhotoVideoId", "originalFileName",
        "thumbhash", "isOffline", "libraryId", "isExternal", "localDateTime",
        "stackId", "duplicateId", "status", "visibility", "width", "height",
        "isEdited",
    },
    "asset_exif": {
        "assetId", "make", "model", "exifImageWidth", "exifImageHeight",
        "fileSizeInByte", "orientation", "dateTimeOriginal", "modifyDate",
        "lensModel", "fNumber", "focalLength", "iso", "latitude", "longitude",
        "city", "state", "country", "description", "fps", "exposureTime",
        "livePhotoCID", "timeZone", "projectionType", "profileDescription",
        "colorspace", "bitsPerSample", "autoStackId", "rating", "tags",
        "lockedProperties",
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
        "sourceType", "isVisible",
    },
    "face_search": {
        "faceId", "embedding",
    },
    "person": {
        "id", "ownerId", "name", "thumbnailPath", "isHidden", "birthDate",
        "faceAssetId", "isFavorite", "color",
    },
    "album_asset": {
        "albumId", "assetId",
    },
}

# Expected vector dimensions for embedding columns. The sidecar copies
# embeddings verbatim via INSERT INTO ... SELECT, so a dimension mismatch
# means Immich changed its ML model and existing embeddings are incompatible.
EXPECTED_VECTOR_DIMS: dict[str, dict[str, int]] = {
    "smart_search": {"embedding": 512},
    "face_search": {"embedding": 512},
}


async def validate_schema(conn: asyncpg.Connection | None = None) -> None:
    """Validate that all required Immich tables and columns exist.

    Queries information_schema.columns and compares against REQUIRED_SCHEMA.
    Also checks for new NOT NULL columns (without defaults) in tables the
    sidecar INSERTs into — these would cause hard failures at runtime.
    Also verifies pgvector embedding dimensions haven't changed.
    Raises SchemaValidationError listing every problem found.
    """
    expected_tables = list(REQUIRED_SCHEMA.keys())
    insert_tables = list(INSERTED_COLUMNS.keys())

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

        # Check for new NOT NULL columns without defaults that the sidecar
        # doesn't supply — these would cause every INSERT to fail.
        required_rows = await c.fetch(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY($1)
              AND is_nullable = 'NO'
              AND column_default IS NULL
              AND is_identity = 'NO'
              AND is_generated = 'NEVER'
            """,
            insert_tables,
        )
        unsupplied: dict[str, list[str]] = {}
        for row in required_rows:
            table = row["table_name"]
            col = row["column_name"]
            if col not in INSERTED_COLUMNS.get(table, set()):
                unsupplied.setdefault(table, []).append(col)

        # Check pgvector embedding dimensions haven't changed.
        dim_mismatches: list[str] = []
        for table, col_dims in EXPECTED_VECTOR_DIMS.items():
            if table in missing_tables:
                continue
            for col, expected_dim in col_dims.items():
                if col in missing_columns.get(table, []):
                    continue
                row = await c.fetchrow(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod) AS col_type
                    FROM pg_attribute a
                    JOIN pg_class cl ON cl.oid = a.attrelid
                    JOIN pg_namespace n ON n.oid = cl.relnamespace
                    WHERE n.nspname = 'public'
                      AND cl.relname = $1
                      AND a.attname = $2
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    """,
                    table,
                    col,
                )
                if row:
                    col_type = row["col_type"]  # e.g. "vector(512)"
                    if f"({expected_dim})" not in col_type:
                        dim_mismatches.append(
                            f"{table}.{col}: expected vector({expected_dim}), got {col_type}"
                        )

        if missing_tables or missing_columns or unsupplied or dim_mismatches:
            parts = []
            if missing_tables:
                parts.append(f"Missing tables: {', '.join(sorted(missing_tables))}")
            for table, cols in sorted(missing_columns.items()):
                parts.append(f"Missing columns in '{table}': {', '.join(cols)}")
            for table, cols in sorted(unsupplied.items()):
                parts.append(
                    f"New required columns in '{table}' not supplied by sidecar: "
                    f"{', '.join(sorted(cols))} — INSERTs will fail"
                )
            for mismatch in dim_mismatches:
                parts.append(f"Embedding dimension mismatch: {mismatch}")
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
