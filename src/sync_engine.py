import logging
from uuid import UUID

from src.album_sync import add_assets_to_album, backfill_album
from src.asset_sync import get_unsynced_source_assets, sync_asset
from src.cleanup import cleanup_deleted_assets, cleanup_reassigned_faces
from src.config import settings
from src.db import transaction
from src.ml_sync import sync_faces_for_asset, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names, sync_person_thumbnails, sync_person_visibility

logger = logging.getLogger(__name__)


async def run_full_sync() -> dict:
    """Run a complete sync cycle: new assets, faces, person updates, cleanups.

    Returns a summary dict of what was done.
    """
    stats = {
        "assets_synced": 0,
        "faces_synced": 0,
        "persons_updated": 0,
        "assets_cleaned": 0,
        "faces_reassigned": 0,
        "persons_cleaned": 0,
        "album_assets_added": 0,
    }

    new_target_ids: list[UUID] = []

    # Phase 1: Sync new assets (per job, in batches to limit memory usage)
    for job in settings.sync_jobs:
        while True:
            async with transaction() as conn:
                source_assets = await get_unsynced_source_assets(conn, job)
                for source in source_assets:
                    target_id = await sync_asset(conn, source, job)
                    if target_id is not None:
                        stats["assets_synced"] += 1
                        new_target_ids.append(target_id)
                        stats["faces_synced"] += await sync_faces_for_asset(
                            conn, source["id"], target_id,
                            job.source_user_id, job.target_user_id,
                        )
            if len(source_assets) < 500:
                break

    # Phase 1b: Album assignment (new + backfill)
    if settings.target_album_uid:
        async with transaction() as conn:
            stats["album_assets_added"] += await add_assets_to_album(conn, new_target_ids)
            stats["album_assets_added"] += await backfill_album(conn)

    # Phase 2: Incremental face sync (catch new/updated faces on existing assets)
    async with transaction() as conn:
        stats["faces_synced"] += await sync_faces_incremental(conn)

    # Phase 3: Sync person metadata changes (names, visibility, thumbnails)
    async with transaction() as conn:
        stats["persons_updated"] += await sync_person_names(conn)
        stats["persons_updated"] += await sync_person_visibility(conn)
        stats["persons_updated"] += await sync_person_thumbnails(conn)

    # Phase 4: Handle deletions and person merges
    async with transaction() as conn:
        stats["assets_cleaned"] = await cleanup_deleted_assets(conn)
        stats["faces_reassigned"] = await cleanup_reassigned_faces(conn)
        stats["persons_cleaned"] = await cleanup_orphaned_persons(conn)

    if any(v > 0 for v in stats.values()):
        logger.info("Sync complete: %s", stats)
    else:
        logger.debug("Sync complete: nothing to do")

    return stats
