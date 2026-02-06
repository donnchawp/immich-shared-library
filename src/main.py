import asyncio
import logging
import sys

from src.config import settings
from src.db import close_pool, execute, fetch_one, init_pool
from src.health import start_health_server, stop_health_server
from src.immich_api import ImmichAPI
from src.sync_engine import run_full_sync

logger = logging.getLogger(__name__)


async def ensure_tracking_tables() -> None:
    """Create the sidecar's tracking tables if they don't exist."""
    await execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_asset_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_asset_id UUID NOT NULL UNIQUE,
            target_asset_id UUID NOT NULL UNIQUE,
            source_user_id UUID NOT NULL,
            target_user_id UUID NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
            source_asset_id UUID PRIMARY KEY,
            reason TEXT NOT NULL,
            skipped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    logger.info("Tracking tables ready")


async def validate_user_and_library_ids() -> None:
    """Validate that configured user and library IDs exist in Immich and are correctly associated.

    Raises RuntimeError if validation fails.
    """
    source_uid = settings.source_uid
    target_uid = settings.target_uid
    target_lid = settings.target_lid

    if source_uid == target_uid:
        raise RuntimeError("source_user_id and target_user_id must be different")

    source_user = await fetch_one(
        'SELECT id FROM "user" WHERE id = $1 AND "deletedAt" IS NULL', source_uid
    )
    if source_user is None:
        raise RuntimeError(f"source_user_id {source_uid} not found or deleted in Immich")

    target_user = await fetch_one(
        'SELECT id FROM "user" WHERE id = $1 AND "deletedAt" IS NULL', target_uid
    )
    if target_user is None:
        raise RuntimeError(f"target_user_id {target_uid} not found or deleted in Immich")

    library = await fetch_one(
        'SELECT id, "ownerId" FROM library WHERE id = $1 AND "deletedAt" IS NULL', target_lid
    )
    if library is None:
        raise RuntimeError(f"target_library_id {target_lid} not found or deleted in Immich")
    if library["ownerId"] != target_uid:
        raise RuntimeError(
            f"target_library_id {target_lid} belongs to user {library['ownerId']}, "
            f"not target_user_id {target_uid}"
        )

    logger.info("Configuration validated: users and library exist and are correctly associated")


def validate_config() -> bool:
    """Validate that required configuration is present."""
    required = [
        ("source_user_id", settings.source_user_id),
        ("target_user_id", settings.target_user_id),
        ("target_library_id", settings.target_library_id),
        ("shared_path_prefix", settings.shared_path_prefix),
        ("immich_api_key", settings.immich_api_key.get_secret_value()),
    ]
    missing = [name for name, val in required if not val]
    if missing:
        logger.error("Missing required configuration: %s", ", ".join(missing))
        return False
    return True


async def sync_loop() -> None:
    """Main sync loop that periodically syncs assets."""
    while True:
        try:
            await run_full_sync()
        except Exception:
            logger.exception("Error in sync loop")
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
    logger.info("Source user: %s", settings.source_user_id)
    logger.info("Target user: %s", settings.target_user_id)
    logger.info("Target library: %s", settings.target_library_id)
    logger.info("Shared path prefix: %s", settings.shared_path_prefix)
    logger.info("Sync interval: %ds", settings.sync_interval_seconds)

    api = ImmichAPI()
    await wait_for_immich(api)

    await init_pool()
    await ensure_tracking_tables()
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
