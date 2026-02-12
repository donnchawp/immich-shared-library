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

# Child tables that must CASCADE DELETE from asset.id.
# cleanup.py deletes from asset and relies on cascades for these.
EXPECTED_CASCADE_CHILDREN: set[str] = {
    "asset_exif", "asset_file", "asset_face", "smart_search", "asset_job_status",
}

# Unique/PK constraints required for ON CONFLICT clauses on Immich tables.
# Each entry is table -> frozenset of column names that must be covered by
# a single unique or primary key constraint.
EXPECTED_UNIQUE_CONSTRAINTS: dict[str, list[frozenset[str]]] = {
    "face_search": [frozenset({"faceId"})],
    "album_asset": [frozenset({"albumId", "assetId"})],
}


async def validate_schema(conn: asyncpg.Connection | None = None) -> None:
    """Validate that all required Immich tables and columns exist.

    Queries information_schema.columns and compares against REQUIRED_SCHEMA.
    Also checks for new NOT NULL columns (without defaults) in tables the
    sidecar INSERTs into — these would cause hard failures at runtime.
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

        # Check CASCADE DELETE from asset to expected child tables.
        constraint_problems: list[str] = []
        cascade_rows = await c.fetch(
            """
            SELECT child.relname AS child_table
            FROM pg_constraint con
            JOIN pg_class child ON con.conrelid = child.oid
            JOIN pg_class parent ON con.confrelid = parent.oid
            JOIN pg_namespace n ON n.oid = parent.relnamespace
            WHERE parent.relname = 'asset'
              AND n.nspname = 'public'
              AND con.contype = 'f'
              AND con.confdeltype = 'c'
            """,
        )
        cascade_children = {row["child_table"] for row in cascade_rows}
        missing_cascades = EXPECTED_CASCADE_CHILDREN - cascade_children
        if missing_cascades:
            constraint_problems.append(
                f"Missing CASCADE DELETE from 'asset' to: "
                f"{', '.join(sorted(missing_cascades))}"
            )

        # Check unique/PK constraints needed for ON CONFLICT clauses.
        for table, required_constraints in EXPECTED_UNIQUE_CONSTRAINTS.items():
            if table in missing_tables:
                continue
            uc_rows = await c.fetch(
                """
                SELECT con.oid,
                       array_agg(a.attname ORDER BY array_position(con.conkey, a.attnum)) AS cols
                FROM pg_constraint con
                JOIN pg_class cl ON con.conrelid = cl.oid
                JOIN pg_namespace n ON n.oid = cl.relnamespace
                JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = ANY(con.conkey)
                WHERE cl.relname = $1
                  AND n.nspname = 'public'
                  AND con.contype IN ('p', 'u')
                GROUP BY con.oid
                """,
                table,
            )
            actual_constraints = [frozenset(row["cols"]) for row in uc_rows]
            for required in required_constraints:
                if required not in actual_constraints:
                    constraint_problems.append(
                        f"Missing unique constraint on '{table}' "
                        f"({', '.join(sorted(required))}) — ON CONFLICT will fail"
                    )

        if missing_tables or missing_columns or unsupplied or constraint_problems:
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
            for problem in constraint_problems:
                parts.append(problem)
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
