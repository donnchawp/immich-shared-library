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

    # Validate album if configured
    if settings.target_album_uid:
        album = await fetch_one(
            'SELECT id, "ownerId", "deletedAt" FROM album WHERE id = $1',
            settings.target_album_uid,
        )
        if album is None:
            raise RuntimeError(
                f"target_album_id {settings.target_album_uid} not found in Immich"
            )
        if album["deletedAt"] is not None:
            raise RuntimeError(
                f"target_album_id {settings.target_album_uid} is deleted"
            )
        job_target_ids = {job.target_user_id for job in settings.sync_jobs}
        if album["ownerId"] not in job_target_ids:
            raise RuntimeError(
                f"target_album_id {settings.target_album_uid} belongs to user "
                f"{album['ownerId']}, not any configured target user {job_target_ids}"
            )

    logger.info("Configuration validated: users, libraries, and album exist and are correctly associated")


def validate_config() -> bool:
    """Validate that required configuration is present."""
    if not settings.immich_api_key.get_secret_value():
        logger.error("Missing required configuration: immich_api_key")
        return False

    # At least one sync source must be configured
    has_external = bool(settings.shared_path_prefix)
    has_upload = bool(settings.upload_source_user_id)

    if not has_external and not has_upload:
        logger.error("At least one of shared_path_prefix or upload_source_user_id must be set")
        return False

    # Validate external library sync requirements
    if has_external:
        required = [
            ("source_user_id", settings.source_user_id),
            ("target_user_id", settings.target_user_id),
            ("target_library_id", settings.target_library_id),
            ("target_path_prefix", settings.target_path_prefix),
        ]
        missing = [name for name, val in required if not val]
        if missing:
            logger.error("External library sync requires: %s", ", ".join(missing))
            return False

    # Validate internal library sync requirements
    if has_upload:
        required = [
            ("upload_target_library_id", settings.upload_target_library_id),
            ("target_upload_path_prefix", settings.target_upload_path_prefix),
        ]
        # Internal library sync needs a target: either its own or the global one
        if not settings.upload_target_user_id and not settings.target_user_id:
            required.append(("upload_target_user_id or target_user_id", ""))
        missing = [name for name, val in required if not val]
        if missing:
            logger.error("Internal library sync requires: %s", ", ".join(missing))
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
    logger.info("Sync interval: %ds", settings.sync_interval_seconds)
    for job in settings.sync_jobs:
        logger.info(
            "Sync job [%s]: source=%s, target=%s, src_prefix=%s, tgt_prefix=%s",
            job.name, job.source_user_id, job.target_user_id,
            job.source_path_prefix, job.target_path_prefix,
        )
    if settings.target_album_uid:
        logger.info("Album: %s", settings.target_album_uid)

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
