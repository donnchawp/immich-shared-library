import logging

from src.asset_sync import get_unsynced_source_assets, sync_asset
from src.cleanup import cleanup_deleted_assets, cleanup_reassigned_faces
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
        "names_updated": 0,
        "assets_cleaned": 0,
        "faces_reassigned": 0,
        "persons_cleaned": 0,
    }

    # Phase 1: Sync new assets
    async with transaction() as conn:
        source_assets = await get_unsynced_source_assets(conn)
        for source in source_assets:
            target_id = await sync_asset(conn, source)
            if target_id is not None:
                stats["assets_synced"] += 1
                # Sync faces for this newly created asset
                face_count = await sync_faces_for_asset(conn, source["id"], target_id)
                stats["faces_synced"] += face_count

    # Phase 2: Incremental face sync (catch new/updated faces on existing assets)
    async with transaction() as conn:
        inc_faces = await sync_faces_incremental(conn)
        stats["faces_synced"] += inc_faces

    # Phase 3: Sync person metadata changes (names, visibility, thumbnails)
    async with transaction() as conn:
        stats["names_updated"] += await sync_person_names(conn)
        stats["names_updated"] += await sync_person_visibility(conn)
        stats["names_updated"] += await sync_person_thumbnails(conn)

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
