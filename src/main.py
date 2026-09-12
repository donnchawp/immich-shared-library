import asyncio
import logging
import sys

import asyncpg

from src.config import settings
from src.db import acquire, close_pool, execute, fetch_one, init_pool, reset_pool, should_reset_pool
from src.health import start_health_server, stop_health_server
from src.immich_api import ImmichAPI
from src.schema import validate_cluster_group, validate_schema
from src.sync_engine import run_full_sync

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 4  # Bump when tracking table schema changes


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
    # Supports the EXISTS semi-joins in person_sync.py (Task 3), which
    # correlate on both target_user_id and source_user_id. target_user_id
    # first: the semi-joins filter on the target side. Without this, those
    # queries are sequential scans that grow with the library (one row per
    # synced asset).
    await execute("""
        CREATE INDEX IF NOT EXISTS _face_sync_asset_map_user_pair_idx
            ON _face_sync_asset_map (target_user_id, source_user_id)
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

    if current < 3:
        await _migrate_v3()

    if current < 4:
        await _migrate_v4()

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


async def _drop_person_map_table(conn: asyncpg.Connection) -> None:
    """Drop the retired _face_sync_person_map table.

    v3.2.0 cluster-group port: person identity is now shared via Immich's
    person_group table, so the sidecar no longer maps source persons to
    target persons (Tasks 3-5 removed every read/write of this table).
    Idempotent (IF EXISTS), so safe to run on every migration pass.

    Takes an explicit connection (rather than using the module-level
    `execute()` pool helper) so the statement itself can be exercised
    directly in tests against a transactional test connection.
    """
    await conn.execute("DROP TABLE IF EXISTS _face_sync_person_map")


async def _migrate_v3() -> None:
    """v3.2.0 cluster-group port: drop the retired person mapping table."""
    async with acquire() as conn:
        await _drop_person_map_table(conn)


async def _strip_copied_face_embeddings(conn) -> tuple[int, int]:
    """Take the sidecar's copied faces out of Immich's recognition candidate pool.

    Copies made before v4 carry a byte-identical clone of the source embedding
    and the source's 'machine-learning' sourceType. Both are wrong, and only
    together: searchFaces() inner-joins face_search, so the clone sits at
    distance 0 from its source and votes a second time when recognition counts
    matches against minFaces — a person in two synced photos reaches a
    threshold of 3 on two real sightings. Deleting the embedding stops that;
    flipping sourceType stops getAllFaces() queueing the copy at all, so it is
    skipped rather than failing on the embedding we just removed.

    Scoped to target assets in _face_sync_asset_map: these are the rows the
    sidecar created, and nothing else in the database should be touched.

    Order matters, and so does the transaction. The reclassification goes
    first: between the two statements the copies are whatever the one already
    run has left them, and 'machine-learning' with no embedding is the single
    state this function exists to prevent — getAllFaces() queues those and
    handleRecognizeFaces fails each one on the embedding that is no longer
    there. 'manual' with an embedding, the other order's intermediate state, is
    inert, because sourceType is checked first. The caller wraps both in one
    transaction so no other connection sees either state, but the order stands
    on its own for the crash case.

    Returns (embeddings deleted, faces reclassified). Idempotent — a second run
    finds nothing left to do.
    """
    faces = await conn.execute(
        """
        UPDATE asset_face af
        SET "sourceType" = 'manual'
        FROM _face_sync_asset_map m
        WHERE af."assetId" = m.target_asset_id
          AND af."sourceType" <> 'manual'
        """
    )
    embeddings = await conn.execute(
        """
        DELETE FROM face_search fs
        USING asset_face af, _face_sync_asset_map m
        WHERE fs."faceId" = af.id
          AND af."assetId" = m.target_asset_id
        """
    )
    return (
        int(embeddings.rsplit(" ", 1)[-1]),
        int(faces.rsplit(" ", 1)[-1]),
    )


async def _migrate_v4() -> None:
    """Stop the sidecar's face copies distorting Immich facial recognition.

    One transaction, not two autocommitted statements: a recognition job that
    runs between them would see the half-migrated state, and asyncpg
    autocommits each statement on a bare acquire().
    """
    async with acquire() as conn:
        async with conn.transaction():
            deleted, reclassified = await _strip_copied_face_embeddings(conn)
    logger.info(
        "Removed %d copied face embeddings and reclassified %d copied faces as "
        "'manual' so Immich's facial recognition ignores them",
        deleted, reclassified,
    )


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


async def sync_loop() -> None:
    """Main sync loop that periodically syncs assets."""
    while True:
        try:
            await run_full_sync()
        except Exception as e:
            logger.exception("Error in sync loop")
            if should_reset_pool(e) or (e.__cause__ and should_reset_pool(e.__cause__)):
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
    # Schema first, migrations second. _migrate_v4 issues a DELETE against
    # Immich's own face_search table, and running that before anything has
    # checked Immich's schema means a version that moved or renamed it gives
    # the operator a raw UndefinedTableError from inside a destructive
    # statement, rather than the curated "Immich was upgraded" message that
    # validate_schema exists to produce. Nothing in validate_schema reads the
    # tracking tables, so the order costs nothing.
    await validate_schema()
    await ensure_tracking_tables()
    # Users and libraries first: both this and validate_cluster_group reject a
    # missing user, but only this one can say which job and which side it came
    # from. validate_cluster_group keeps its own check for the per-cycle call
    # in sync_engine, where this function never runs.
    await validate_user_and_library_ids()
    async with acquire() as conn:
        await validate_cluster_group(conn, settings.configured_user_ids)

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
