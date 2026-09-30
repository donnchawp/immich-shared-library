import logging
from uuid import UUID

from src.album_sync import add_assets_to_album, backfill_album
from src.asset_sync import find_duplicate_filenames, get_unsynced_source_assets, record_skipped_duplicates, sync_asset
from src.cleanup import cleanup_deleted_assets, cleanup_reassigned_faces, cleanup_stale_mappings
from src.config import settings
from src.db import is_connection_error, transaction
from src.ml_sync import sync_faces_for_asset_guarded, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names, sync_person_thumbnails
from src.schema import SchemaValidationError, validate_cluster_group, validate_schema

logger = logging.getLogger(__name__)

# Phase 1 reads unsynced source assets in batches of this size. A short batch
# means the source is exhausted and the job is done.
BATCH_SIZE = 500


async def _phase(name: str, coro):
    """Run one phase, keeping its failure from costing the phases after it.

    Phase 4 has had this since the savepoint work; the other phases had not,
    and the asymmetry was a live version of the bug that moved the stale-map
    prune to Phase 0. An exception here propagated out of run_full_sync, so a
    deterministic failure anywhere above Phase 4 — sync_person_thumbnails
    hitting a full disk, say, which does filesystem work — meant
    cleanup_deleted_assets never ran again, every cycle, silently.

    Each phase already owns its transaction, so the rollback has happened by
    the time we get here; this only decides whether the cycle continues.

    Two exceptions are re-raised rather than swallowed:

    - SchemaValidationError means Immich's schema moved under us. Every later
      phase is more SQL against a shape we no longer understand, so the cycle
      stops and startup validation gets to say so on the next one.
    - A broken connection has to reach sync_loop, which resets the pool.
      Swallowing it would leave every later phase failing against a dead
      connection and the pool never rebuilt.
    """
    try:
        return await coro
    except SchemaValidationError:
        raise
    except Exception as e:
        if is_connection_error(e) or (e.__cause__ and is_connection_error(e.__cause__)):
            raise
        logger.exception("Phase %s failed; the rest of the cycle stands", name)
        return None


async def _cleanup_step(conn, fn) -> int:
    """Run one Phase 4 cleanup inside its own savepoint. Returns its count, or 0.

    The steps share a transaction but must not share a fate.
    cleanup_deleted_assets removes hardlinked thumbnails *before* deleting the
    rows that name them, and the filesystem does not roll back — so a failure
    in a later step would restore asset rows whose files are already gone and
    leave Immich showing broken assets, indefinitely if the failure is
    deterministic. This is the same reasoning _sync_faces_guarded applies to
    Phase 1; it just hadn't been carried down here.

    A lost connection is re-raised, not absorbed. A savepoint contains a failed
    statement, not a dead socket: nothing already done in this transaction will
    commit, and every later step would fail on the same closed connection.
    Swallowing it logged "the rest of the cycle stands" for a cycle that stood
    nowhere, then ran the next step into "connection has been released back to
    the pool". Raised, it reaches sync_loop, which resets the pool.
    """
    try:
        async with conn.transaction():
            return await fn(conn)
    except Exception:
        if conn.is_closed():
            raise
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

    # Savepointed like everything else in Phase 1. It is a constraint-free
    # single-table UPDATE, so the realistic failure is the connection going
    # away rather than the statement itself -- but it is the one write left in
    # this phase that could abort all 500 assets' mappings while their
    # hardlinks stay on disk, which is the harm the surrounding docstring is
    # about.
    async with conn.transaction():
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
    # Shared across jobs deliberately: validation is per cycle, not per job. A
    # dict rather than a bool because _sync_job_assets is what discovers there
    # is anything to validate against, and a bool passed by value would leave
    # every job after the first re-running it.
    validated = {"schema": False}
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

    # Phase 1: Sync new assets (per job, in batches to limit memory usage).
    # Guarded per job, not per phase: one job's source library going bad must
    # not cost every job behind it in the list its whole cycle.
    for job in settings.sync_jobs:
        job_ids = await _phase(f"1 ({job.name})", _sync_job_assets(job, stats, validated))
        if job_ids:
            job_target_ids[job.name] = job_ids

    # Phase 1b: Per-job album assignment (new + backfill)
    for job in settings.sync_jobs:
        if not job.album_id:
            continue
        await _phase(f"1b ({job.name})", _assign_album(job, job_target_ids, stats))

    # Phase 2: Incremental face sync (catch new/updated faces on existing assets)
    await _phase("2", _sync_faces_phase(stats))

    # Phase 3: Sync person metadata changes (names and thumbnails).
    # Visibility (isHidden) is deliberately absent: it is per-user, and a
    # boolean has no "unset" sentinel, so there is no fill-only option.
    await _phase("3", _sync_person_metadata(stats))

    # Phase 4: Handle deletions and face/person drift. Stale mappings were
    # pruned in Phase 0, before any phase read the map. Each step is
    # savepointed — see _cleanup_step for why they must not share a fate.
    await _phase("4", _cleanup_phase(stats))

    if any(v > 0 for v in stats.values()):
        logger.info("Sync complete: %s", stats)
    else:
        logger.debug("Sync complete: nothing to do")

    return stats


async def _sync_job_assets(job, stats: dict, validated: dict) -> list[UUID]:
    """Phase 1 for one job. Returns the target asset ids it created."""
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
            # limit=BATCH_SIZE, not the query's own default: the loop below
            # decides the source is exhausted by comparing the row count
            # against BATCH_SIZE, so if the two ever disagree it either exits
            # after one batch every cycle or never takes the short-batch exit
            # at all -- and in the first case the no-progress guard below
            # becomes unreachable.
            source_assets = await get_unsynced_source_assets(conn, job, limit=BATCH_SIZE)
            if source_assets and not validated["schema"]:
                await validate_schema(conn)
                await validate_cluster_group(conn, settings.configured_user_ids)
                validated["schema"] = True

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
    return job_ids


async def _assign_album(job, job_target_ids: dict, stats: dict) -> None:
    """Phase 1b for one job."""
    async with transaction() as conn:
        new_ids = job_target_ids.get(job.name, [])
        stats["album_assets_added"] += await add_assets_to_album(conn, new_ids, job.album_id)
        stats["album_assets_added"] += await backfill_album(conn, job.album_id, job.target_user_id)


async def _sync_faces_phase(stats: dict) -> None:
    async with transaction() as conn:
        stats["faces_synced"] += await sync_faces_incremental(conn)


async def _sync_person_metadata(stats: dict) -> None:
    async with transaction() as conn:
        stats["persons_updated"] += await sync_person_names(conn)
        stats["persons_updated"] += await sync_person_thumbnails(conn)


async def _cleanup_phase(stats: dict) -> None:
    async with transaction() as conn:
        stats["assets_cleaned"] = await _cleanup_step(conn, cleanup_deleted_assets)
        stats["faces_reassigned"] = await _cleanup_step(conn, cleanup_reassigned_faces)
        stats["persons_cleaned"] = await _cleanup_step(conn, cleanup_orphaned_persons)
