import asyncio
import logging
import sys

from src.config import settings
from src.db import close_pool, execute, init_pool
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
    logger.info("Tracking tables ready")


def validate_config() -> bool:
    """Validate that required configuration is present."""
    required = [
        ("source_user_id", settings.source_user_id),
        ("target_user_id", settings.target_user_id),
        ("target_library_id", settings.target_library_id),
        ("shared_path_prefix", settings.shared_path_prefix),
        ("immich_api_key", settings.immich_api_key),
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

    # Wait for Immich to be ready
    await wait_for_immich(api)

    # Initialize database
    await init_pool()
    await ensure_tracking_tables()

    # Start health check server
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
