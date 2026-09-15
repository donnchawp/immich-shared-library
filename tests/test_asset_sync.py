"""Phase 1's crash-recovery path, which runs before any file is touched.

sync_asset's happy path creates hardlinks and is covered by the manual
integration test against a live instance. What is worth pinning here is the
branch that takes no filesystem action at all: the asset already exists, so
only the mapping has to be rebuilt — and the ways that INSERT can fail are the
ways the whole cycle used to wedge.
"""
from uuid import uuid4

import asyncpg

import src.asset_sync as asset_sync
from src.asset_sync import sync_asset
from src.config import SyncJob
from tests.conftest import make_asset


async def _library(conn, owner_id):
    return await conn.fetchval(
        """
        INSERT INTO library (id, name, "ownerId", "importPaths", "exclusionPatterns")
        VALUES ($1, 'test', $2, '{}', '{}') RETURNING id
        """,
        uuid4(), owner_id,
    )


async def _job(conn, src, tgt):
    return SyncJob(
        name="test",
        source_user_id=src,
        target_user_id=tgt,
        target_library_id=await _library(conn, tgt),
        source_path_prefix="/external_library/source/",
        target_path_prefix="/external_library/target/",
    )


async def _existing_target(conn, job, path):
    """A target asset at `path`, as a previous cycle would have left it."""
    aid = await make_asset(conn, job.target_user_id, original_path=path)
    await conn.execute(
        'UPDATE asset SET "libraryId" = $1 WHERE id = $2', job.target_library_id, aid
    )
    return aid


async def test_recovers_a_lost_mapping_without_creating_a_second_asset(conn, pair):
    """The crash-recovery branch: asset survived, mapping did not."""
    cg, src, tgt = pair
    job = await _job(conn, src, tgt)

    source = await conn.fetchrow(
        "SELECT * FROM asset WHERE id = $1",
        await make_asset(conn, src, original_path="/external_library/source/a.jpg"),
    )
    existing = await _existing_target(conn, job, "/external_library/target/a.jpg")

    assert await sync_asset(conn, source, job) == existing
    assert await conn.fetchval(
        "SELECT count(*) FROM _face_sync_asset_map WHERE target_asset_id = $1", existing
    ) == 1


async def test_a_target_already_mapped_to_another_source_does_not_abort_the_batch(conn, pair):
    """The wedge that outlived the Phase 0 fix, one function away from it.

    _face_sync_asset_map has two unique constraints, and the recovery INSERT
    used to name only one of them in its ON CONFLICT — so a violation of the
    other, target_asset_id, escaped as an exception. Bare inside the batch
    transaction it aborted all 500 assets of that batch (rolling their
    mappings back while their hardlinks stayed on disk) and then escaped the
    cycle entirely, before the Phase 4 cleanup that would have cleared the
    stale mapping could run. Every cycle after failed identically.

    Reached when a source asset is removed from the external library and
    rescanned: Immich gives it a new id at the same path, so the remap finds
    the target that the *old* source id still claims.
    """
    cg, src, tgt = pair
    job = await _job(conn, src, tgt)

    existing = await _existing_target(conn, job, "/external_library/target/a.jpg")
    old_source = await make_asset(conn, src, original_path="/external_library/source/old.jpg")
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id)
        VALUES ($1, $2, $3, $4)
        """,
        old_source, existing, src, tgt,
    )

    rescanned = await conn.fetchrow(
        "SELECT * FROM asset WHERE id = $1",
        await make_asset(conn, src, original_path="/external_library/source/a.jpg"),
    )

    # No exception, and the batch's transaction is still usable afterwards.
    assert await sync_asset(conn, rescanned, job) is None
    assert await conn.fetchval("SELECT 1") == 1

    # The old mapping is untouched; Phase 4 is what clears it.
    assert await conn.fetchval(
        "SELECT source_asset_id FROM _face_sync_asset_map WHERE target_asset_id = $1",
        existing,
    ) == old_source


async def test_a_path_that_escapes_the_target_prefix_is_contained_to_its_own_asset(conn, pair):
    """The one input _remap_asset_path rejects must not take the batch with it.

    A path outside the job's prefix is *not* an error — it is returned
    unchanged. What raises is a path that normalizes out of the target prefix,
    which is the traversal guard doing its job. That raise used to happen
    before the savepoint opened, so it propagated straight out of sync_asset
    into the Phase 1 loop and aborted every other asset in the batch.
    """
    cg, src, tgt = pair
    job = await _job(conn, src, tgt)

    source = await conn.fetchrow(
        "SELECT * FROM asset WHERE id = $1",
        await make_asset(
            conn, src, original_path="/external_library/source/../../etc/passwd.jpg"
        ),
    )

    assert await sync_asset(conn, source, job) is None
    assert await conn.fetchval("SELECT 1") == 1
    assert await conn.fetchval("SELECT count(*) FROM _face_sync_asset_map") == 0


async def test_the_watermark_comes_from_the_database_clock(conn, pair):
    """synced_at is compared against asset_face."updatedAt", which Postgres sets.

    Taking it from the sidecar container's clock instead means skew puts the
    watermark in the future, and every source face updated inside that gap is
    permanently invisible to Phase 2, silently. Comparing it against the
    database's own clock catches the container-clock version without having to
    skew anything: under it the two differ by the drift, and drift is exactly
    what is unbounded.
    """
    cg, src, tgt = pair
    job = await _job(conn, src, tgt)

    source = await conn.fetchrow(
        "SELECT * FROM asset WHERE id = $1",
        await make_asset(conn, src, original_path="/external_library/source/w.jpg"),
    )
    existing = await _existing_target(conn, job, "/external_library/target/w.jpg")

    assert await sync_asset(conn, source, job) == existing

    drift = await conn.fetchval(
        """
        SELECT abs(extract(epoch FROM (NOW() - synced_at)))
        FROM _face_sync_asset_map WHERE target_asset_id = $1
        """,
        existing,
    )
    assert drift < 1, f"synced_at is {drift}s from the database clock"


async def test_a_non_checksum_unique_violation_is_not_recorded_as_a_duplicate(
    conn, pair, monkeypatch
):
    """_face_sync_skipped is permanent and never retried.

    Every UniqueViolationError inside the savepoint used to land in the
    duplicate-checksum branch, so an asset that was not a duplicate could be
    excluded from syncing forever, under a reason naming the wrong cause. A
    violation carrying no constraint_name must not be read as a checksum
    collision either -- the branch keys off an exact match.
    """
    cg, src, tgt = pair
    job = await _job(conn, src, tgt)

    source = await conn.fetchrow(
        "SELECT * FROM asset WHERE id = $1",
        await make_asset(conn, src, original_path="/external_library/source/u.jpg"),
    )

    async def unique_violation_on_something_else(*a, **kw):
        raise asyncpg.UniqueViolationError("duplicate key value violates something else")

    monkeypatch.setattr(
        asset_sync, "_sync_asset_files", unique_violation_on_something_else
    )

    assert await sync_asset(conn, source, job) is None

    skipped = await conn.fetchval(
        "SELECT count(*) FROM _face_sync_skipped WHERE source_asset_id = $1", source["id"]
    )
    assert skipped == 0, "a non-checksum violation was recorded as a permanent duplicate"
    assert await conn.fetchval("SELECT 1") == 1, "the transaction is unusable"
