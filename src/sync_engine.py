import logging
from uuid import UUID

from src.album_sync import add_assets_to_album, backfill_album
from src.asset_sync import find_duplicate_filenames, get_unsynced_source_assets, record_skipped_duplicates, sync_asset
from src.cleanup import cleanup_deleted_assets, cleanup_reassigned_faces
from src.config import settings
from src.db import transaction
from src.ml_sync import sync_faces_for_asset, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names, sync_person_thumbnails, sync_person_visibility
from src.schema import validate_schema

logger = logging.getLogger(__name__)


async def run_full_sync() -> dict:
    """Run a complete sync cycle: new assets, faces, person updates, cleanups.

    Returns a summary dict of what was done.
    """
    await validate_schema()

    stats = {
        "assets_synced": 0,
        "assets_skipped_duplicate": 0,
        "faces_synced": 0,
        "persons_updated": 0,
        "assets_cleaned": 0,
        "faces_reassigned": 0,
        "persons_cleaned": 0,
        "album_assets_added": 0,
    }

    # Track new target asset IDs per job (for per-job album assignment)
    job_target_ids: dict[str, list[UUID]] = {}

    # Phase 1: Sync new assets (per job, in batches to limit memory usage)
    for job in settings.sync_jobs:
        job_ids: list[UUID] = []
        while True:
            async with transaction() as conn:
                source_assets = await get_unsynced_source_assets(conn, job)

                # Duplicate detection: skip source assets already in target by filename + capture time
                duplicates = await find_duplicate_filenames(conn, source_assets, job)
                if duplicates:
                    await record_skipped_duplicates(conn, duplicates)
                    stats["assets_skipped_duplicate"] += len(duplicates)
                    logger.warning(
                        "Job %s: skipping %d duplicate(s) by filename+date",
                        job.name, len(duplicates),
                    )

                for source in source_assets:
                    if source["id"] in duplicates:
                        logger.debug("Skipping duplicate asset %s (%s)", source["id"], source["originalFileName"])
                        continue
                    target_id = await sync_asset(conn, source, job)
                    if target_id is not None:
                        stats["assets_synced"] += 1
                        job_ids.append(target_id)
                        stats["faces_synced"] += await sync_faces_for_asset(
                            conn, source["id"], target_id,
                            job.source_user_id, job.target_user_id,
                        )
            if len(source_assets) < 500:
                break
        if job_ids:
            job_target_ids[job.name] = job_ids

    # Phase 1b: Per-job album assignment (new + backfill)
    for job in settings.sync_jobs:
        if not job.album_id:
            continue
        async with transaction() as conn:
            new_ids = job_target_ids.get(job.name, [])
            stats["album_assets_added"] += await add_assets_to_album(conn, new_ids, job.album_id)
            stats["album_assets_added"] += await backfill_album(conn, job.album_id, job.target_user_id)

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
