import asyncio
import logging
import sys

import asyncpg

from src.config import settings
from src.db import close_pool, execute, fetch_one, init_pool, reset_pool
from src.health import start_health_server, stop_health_server
from src.immich_api import ImmichAPI
from src.schema import validate_schema
from src.sync_engine import run_full_sync

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 2  # Bump when tracking table schema changes


async def ensure_tracking_tables() -> None:
    """Create the sidecar's tracking tables if they don't exist, and run migrations."""
    # Version tracking table (created first so migrations can read it)
    await execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    await execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_asset_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_asset_id UUID NOT NULL,
            target_asset_id UUID NOT NULL UNIQUE,
            source_user_id UUID NOT NULL,
            target_user_id UUID NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (source_asset_id, target_user_id)
        )
    """)
    await execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_person_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_person_id UUID NOT NULL,
            target_person_id UUID NOT NULL,
            source_user_id UUID NOT NULL,
            target_user_id UUID NOT NULL,
            UNIQUE (source_person_id, target_user_id)
        )
    """)
    await execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_skipped (
            source_asset_id UUID NOT NULL,
            target_user_id UUID NOT NULL,
            reason TEXT NOT NULL,
            skipped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (source_asset_id, target_user_id)
        )
    """)

    await _run_migrations()
    logger.info("Tracking tables ready (schema version %d)", SCHEMA_VERSION)


async def _run_migrations() -> None:
    """Run pending schema migrations based on stored version."""
    row = await fetch_one(
        "SELECT value FROM _face_sync_meta WHERE key = 'schema_version'"
    )
    current = int(row["value"]) if row else 1

    if current >= SCHEMA_VERSION:
        return

    if current < 2:
        await _migrate_v2()

    await execute(
        """
        INSERT INTO _face_sync_meta (key, value) VALUES ('schema_version', $1)
        ON CONFLICT (key) DO UPDATE SET value = $1
        """,
        str(SCHEMA_VERSION),
    )
    logger.info("Migrated tracking tables from v%d to v%d", current, SCHEMA_VERSION)


async def _migrate_v2() -> None:
    """GH issue #1: allow syncing one source asset to multiple target users.

    - _face_sync_asset_map: UNIQUE(source_asset_id) -> UNIQUE(source_asset_id, target_user_id)
    - _face_sync_skipped: add target_user_id, change PK to composite
    """
    # Fix asset map unique constraint
    await execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '_face_sync_asset_map_source_asset_id_key'
                  AND conrelid = '_face_sync_asset_map'::regclass
            ) THEN
                ALTER TABLE _face_sync_asset_map
                    DROP CONSTRAINT _face_sync_asset_map_source_asset_id_key;
                ALTER TABLE _face_sync_asset_map
                    ADD CONSTRAINT _face_sync_asset_map_source_target_user_key
                    UNIQUE (source_asset_id, target_user_id);
            END IF;
        END $$
    """)

    # Recreate skipped table with target_user_id (skip data is just an
    # optimisation — losing it means a one-time re-check of skipped assets)
    await execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = '_face_sync_skipped'
                  AND column_name = 'target_user_id'
            ) THEN
                DROP TABLE _face_sync_skipped;
                CREATE TABLE _face_sync_skipped (
                    source_asset_id UUID NOT NULL,
                    target_user_id UUID NOT NULL,
                    reason TEXT NOT NULL,
                    skipped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (source_asset_id, target_user_id)
                );
            END IF;
        END $$
    """)


async def validate_user_and_library_ids() -> None:
    """Validate that configured user and library IDs exist in Immich and are correctly associated.

    Raises RuntimeError if validation fails.
    """
    # Validate each sync job's users and library
    for job in settings.sync_jobs:
        source_user = await fetch_one(
            'SELECT id FROM "user" WHERE id = $1 AND "deletedAt" IS NULL', job.source_user_id
        )
        if source_user is None:
            raise RuntimeError(
                f"[{job.name}] source_user_id {job.source_user_id} not found or deleted in Immich"
            )

        target_user = await fetch_one(
            'SELECT id FROM "user" WHERE id = $1 AND "deletedAt" IS NULL', job.target_user_id
        )
        if target_user is None:
            raise RuntimeError(
                f"[{job.name}] target_user_id {job.target_user_id} not found or deleted in Immich"
            )

        if job.source_user_id == job.target_user_id:
            raise RuntimeError(
                f"[{job.name}] source_user_id and target_user_id must be different"
            )

        library = await fetch_one(
            'SELECT id, "ownerId" FROM library WHERE id = $1 AND "deletedAt" IS NULL',
            job.target_library_id,
        )
        if library is None:
            raise RuntimeError(
                f"[{job.name}] target_library_id {job.target_library_id} not found or deleted in Immich"
            )
        if library["ownerId"] != job.target_user_id:
            raise RuntimeError(
                f"[{job.name}] target_library_id {job.target_library_id} belongs to user "
                f"{library['ownerId']}, not target_user_id {job.target_user_id}"
            )

        # Validate per-job album if configured
        if job.album_id:
            # Since Immich v3, album ownership lives in album_user (role='owner')
            # rather than album."ownerId"
            album = await fetch_one(
                """
                SELECT a.id, a."deletedAt", au."userId" AS owner_id
                FROM album a
                LEFT JOIN album_user au ON au."albumId" = a.id AND au.role = 'owner'
                WHERE a.id = $1
                """,
                job.album_id,
            )
            if album is None:
                raise RuntimeError(
                    f"[{job.name}] album_id {job.album_id} not found in Immich"
                )
            if album["deletedAt"] is not None:
                raise RuntimeError(
                    f"[{job.name}] album_id {job.album_id} is deleted"
                )
            if album["owner_id"] != job.target_user_id:
                raise RuntimeError(
                    f"[{job.name}] album_id {job.album_id} belongs to user "
                    f"{album['owner_id']}, not target_user_id {job.target_user_id}"
                )

    logger.info("Configuration validated: users, libraries, and albums exist and are correctly associated")


def validate_config() -> bool:
    """Validate that required configuration is present."""
    if not settings.immich_api_key.get_secret_value():
        logger.error("Missing required configuration: immich_api_key")
        return False

    try:
        jobs = settings.sync_jobs
    except (ValueError, FileNotFoundError) as e:
        logger.error("Configuration error: %s", e)
        return False

    if not jobs:
        logger.error("No sync jobs configured. Create a config.yaml or set env vars (see README).")
        return False

    return True


def _is_connection_error(exc: Exception) -> bool:
    """Check if an exception indicates a broken database connection."""
    return isinstance(exc, (
        OSError,  # covers ConnectionResetError, socket.gaierror, etc.
        asyncpg.exceptions.ConnectionDoesNotExistError,
        asyncpg.exceptions.InterfaceError,
    ))


async def sync_loop() -> None:
    """Main sync loop that periodically syncs assets."""
    while True:
        try:
            await run_full_sync()
        except Exception as e:
            logger.exception("Error in sync loop")
            if _is_connection_error(e) or (e.__cause__ and _is_connection_error(e.__cause__)):
                logger.info("Detected connection error, resetting database pool")
                try:
                    await reset_pool()
                except Exception:
                    logger.exception("Failed to reset database pool, will retry next cycle")
        await asyncio.sleep(settings.sync_interval_seconds)


async def wait_for_immich(api: ImmichAPI, max_retries: int = 30, delay: float = 10.0) -> None:
    """Wait for the Immich server to become available."""
    for i in range(max_retries):
        if await api.health_check():
            logger.info("Immich server is available")
            return
        logger.info("Waiting for Immich server (attempt %d/%d)...", i + 1, max_retries)
        await asyncio.sleep(delay)
    raise RuntimeError("Immich server did not become available")


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not validate_config():
        sys.exit(1)

    logger.info("Starting immich-shared-library sidecar")
    logger.info("Sync interval: %ds", settings.sync_interval_seconds)
    for job in settings.sync_jobs:
        logger.info(
            "Sync job [%s]: source=%s, target=%s, src_prefix=%s, tgt_prefix=%s, album=%s",
            job.name, job.source_user_id, job.target_user_id,
            job.source_path_prefix, job.target_path_prefix,
            job.album_id or "none",
        )

    api = ImmichAPI()
    await wait_for_immich(api)

    await init_pool()
    await ensure_tracking_tables()
    await validate_schema()
    await validate_user_and_library_ids()

    await start_health_server()

    try:
        await sync_loop()
    except asyncio.CancelledError:
        logger.info("Shutting down...")
    finally:
        await stop_health_server()
        await api.close()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
