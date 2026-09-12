import logging
from uuid import UUID

from src.album_sync import add_assets_to_album, backfill_album
from src.asset_sync import find_duplicate_filenames, get_unsynced_source_assets, record_skipped_duplicates, sync_asset
from src.cleanup import cleanup_deleted_assets, cleanup_reassigned_faces, cleanup_stale_mappings
from src.config import settings
from src.db import transaction
from src.ml_sync import sync_faces_for_asset_guarded, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names, sync_person_thumbnails
from src.schema import validate_cluster_group, validate_schema

logger = logging.getLogger(__name__)

# Phase 1 reads unsynced source assets in batches of this size. A short batch
# means the source is exhausted and the job is done.
BATCH_SIZE = 500


async def _cleanup_step(conn, fn) -> int:
    """Run one Phase 4 cleanup inside its own savepoint. Returns its count, or 0.

    The steps share a transaction but must not share a fate.
    cleanup_deleted_assets removes hardlinked thumbnails *before* deleting the
    rows that name them, and the filesystem does not roll back — so a failure
    in a later step would restore asset rows whose files are already gone and
    leave Immich showing broken assets, indefinitely if the failure is
    deterministic. This is the same reasoning _sync_faces_guarded applies to
    Phase 1; it just hadn't been carried down here.
    """
    try:
        async with conn.transaction():
            return await fn(conn)
    except Exception:
        logger.exception("Phase 4 step '%s' failed; the rest of the cycle stands", fn.__name__)
        return 0


async def _sync_faces_guarded(
    conn,
    source_asset_id: UUID,
    target_asset_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
) -> int:
    """Phase 1's face copy, with Phase 1's recovery policy. Returns faces synced.

    sync_asset releases its savepoint before returning, so this work would
    otherwise run bare inside the batch transaction — and it writes to
    `person`, which carries two foreign keys. One failure (Immich's
    deleteEmptyGroups dropping a person_group between our read and
    ensure_target_person's INSERT, say) would abort all 500 assets in the
    batch, rolling back their mappings while their hardlinked thumbnails
    stayed on disk.

    The asset itself is kept on failure — it is complete, only its faces are
    missing — and the mapping's watermark is rewound instead. This is where
    Phase 1 differs from Phase 2: sync_asset has just stamped `synced_at` as
    now, and Phase 2 only revisits pairs holding a source face newer than
    that, so without the rewind the asset would stay faceless forever.
    """
    count = await sync_faces_for_asset_guarded(
        conn, source_asset_id, target_asset_id, source_user_id, target_user_id,
    )
    if count is not None:
        return count

    await conn.execute(
        """
        UPDATE _face_sync_asset_map SET synced_at = 'epoch'
        WHERE source_asset_id = $1 AND target_user_id = $2
        """,
        source_asset_id,
        target_user_id,
    )
    logger.warning(
        "Asset %s synced without faces; watermark rewound for Phase 2 to retry",
        source_asset_id,
    )
    return 0


async def run_full_sync() -> dict:
    """Run a complete sync cycle: new assets, faces, person updates, cleanups.

    Returns a summary dict of what was done.
    """
    schema_validated = False
    stats = {
        "assets_synced": 0,
        "assets_skipped_duplicate": 0,
        "faces_synced": 0,
        "persons_updated": 0,
        "assets_cleaned": 0,
        "faces_reassigned": 0,
        "persons_cleaned": 0,
        "album_assets_added": 0,
        "stale_mappings_pruned": 0,
    }

    # Track new target asset IDs per job (for per-job album assignment)
    job_target_ids: dict[str, list[UUID]] = {}

    # Phase 0: Prune mappings whose target asset is gone, before anything reads
    # the map.
    #
    # This runs first so the rest of the cycle can assume every mapping points
    # at a live target asset. It used to run at the end of Phase 4, which meant
    # a hard-deleted target wedged the sidecar permanently: Phase 2 hit a
    # foreign key violation inserting a face for the vanished asset, the cycle
    # aborted, and the prune that would have fixed it never ran — identically,
    # every cycle after. Consumers still guard themselves, but as belt and
    # braces rather than as the only thing standing between a deleted photo and
    # a stuck sidecar.
    async with transaction() as conn:
        stats["stale_mappings_pruned"] = await cleanup_stale_mappings(conn)

    # Phase 1: Sync new assets (per job, in batches to limit memory usage)
    for job in settings.sync_jobs:
        job_ids: list[UUID] = []
        while True:
            # A batch that changes nothing durable returns the identical rows
            # next time round: sync_asset's catch-all returns None without
            # recording anything, so a full batch of persistently failing
            # assets (a path that won't remap, a NOT NULL column that slipped
            # past validation) would spin here forever, inside a process whose
            # only other liveness signal is a health server that keeps
            # answering. Only a duplicate record or a successful sync counts
            # as progress, because only those shrink the next query's result.
            progressed = False
            async with transaction() as conn:
                source_assets = await get_unsynced_source_assets(conn, job)
                if source_assets and not schema_validated:
                    await validate_schema(conn)
                    await validate_cluster_group(conn, settings.configured_user_ids)
                    schema_validated = True

                # Duplicate detection: skip source assets already in target by filename + capture time
                duplicates = await find_duplicate_filenames(conn, source_assets, job)
                if duplicates:
                    await record_skipped_duplicates(conn, duplicates, job.target_user_id)
                    stats["assets_skipped_duplicate"] += len(duplicates)
                    progressed = True
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
                        progressed = True
                        stats["assets_synced"] += 1
                        job_ids.append(target_id)
                        stats["faces_synced"] += await _sync_faces_guarded(
                            conn, source["id"], target_id,
                            job.source_user_id, job.target_user_id,
                        )
            if len(source_assets) < BATCH_SIZE:
                break
            if not progressed:
                logger.error(
                    "Job %s: a full batch of %d assets synced nothing; stopping this "
                    "cycle's asset sync. The next cycle retries, so a transient fault "
                    "recovers on its own — a persistent one needs the logged exceptions.",
                    job.name, len(source_assets),
                )
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

    # Phase 3: Sync person metadata changes (names and thumbnails).
    # Visibility (isHidden) is deliberately absent: it is per-user, and a
    # boolean has no "unset" sentinel, so there is no fill-only option.
    async with transaction() as conn:
        stats["persons_updated"] += await sync_person_names(conn)
        stats["persons_updated"] += await sync_person_thumbnails(conn)

    # Phase 4: Handle deletions and face/person drift. Stale mappings were
    # pruned in Phase 0, before any phase read the map. Each step is
    # savepointed — see _cleanup_step for why they must not share a fate.
    async with transaction() as conn:
        stats["assets_cleaned"] = await _cleanup_step(conn, cleanup_deleted_assets)
        stats["faces_reassigned"] = await _cleanup_step(conn, cleanup_reassigned_faces)
        stats["persons_cleaned"] = await _cleanup_step(conn, cleanup_orphaned_persons)

    if any(v > 0 for v in stats.values()):
        logger.info("Sync complete: %s", stats)
    else:
        logger.debug("Sync complete: nothing to do")

    return stats
