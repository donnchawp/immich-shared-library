"""End-to-end face-sync convergence, and proof that _face_sync_person_map is gone.

v3.2.0 cluster-group port: person identity is shared via Immich's person_group
table, so the sidecar no longer maps source persons to target persons. Tasks
3-5 removed every read/write of `_face_sync_person_map`; this task retires
the table itself.
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest

from src import main as main_module
from src.cleanup import cleanup_reassigned_faces
from src.main import _drop_person_map_table
from src.ml_sync import sync_faces_for_asset, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names
from src.schema import SchemaValidationError
from src import sync_engine
from src.sync_engine import _sync_faces_guarded
from tests.conftest import (
    make_asset, make_face, make_person, make_person_group,
    make_synced_pair, make_user,
)


async def test_full_face_flow_converges(conn, pair):
    """Sync a face, rename at source, reassign at source -- all must converge."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt,
        source_asset_id=src_asset, target_asset_id=tgt_asset,
    )
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt) == 1

    # Source user names the person later
    await conn.execute(
        'UPDATE person SET name = $1 WHERE "ownerId" = $2 AND "personGroupId" = $3',
        "Jacinta", src, pg,
    )
    assert await sync_person_names(conn) == 1

    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Jacinta"

    # Second cycle must be a no-op everywhere
    assert await sync_person_names(conn) == 0
    assert await cleanup_reassigned_faces(conn) == 0
    assert await cleanup_orphaned_persons(conn) == 0


async def test_drop_person_map_table_removes_it(conn):
    """The migration step must actually drop _face_sync_person_map.

    Asserting against `to_regclass` on the ambient scratch DB alone would be
    vacuous either way: a same-session re-run would find the table already
    dropped by an earlier test, and whether a fresh `make testdb` has it at
    all depends on when the fixture was last dumped. Neither state proves
    anything about the sidecar's own code.

    So the table is seeded here rather than assumed, like the migration tests
    below do. It used to lean on the fixture still carrying it, which was a
    trap: the sidecar drops that table from any live instance it runs against,
    so the first `make schema-dump` after deploying this branch took it out of
    the fixture and broke this test with an error about the wrong thing.

    This exercises the real drop statement (`src.main._drop_person_map_table`)
    against the per-test transactional connection and checks the *transition*:
    present beforehand, so a no-op implementation fails, and gone afterward.
    The transaction rolls back, so the shared scratch DB is untouched.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_person_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        )
    """)
    before = await conn.fetchval("SELECT to_regclass('_face_sync_person_map')")
    assert before is not None

    await _drop_person_map_table(conn)

    after = await conn.fetchval("SELECT to_regclass('_face_sync_person_map')")
    assert after is None


async def test_drop_person_map_table_is_idempotent(conn):
    """Safe to run on every startup, including once the table is already gone."""
    await _drop_person_map_table(conn)
    await _drop_person_map_table(conn)  # must not raise (IF EXISTS)

    exists = await conn.fetchval("SELECT to_regclass('_face_sync_person_map')")
    assert exists is None


@pytest.fixture
def route_main_db_calls_through_conn(conn, monkeypatch):
    """Make `src.main`'s startup DB calls hit the test's own transactional
    connection, so the real migration entry point can be driven end to end.

    `ensure_tracking_tables` / `_run_migrations` / `_migrate_v2` /
    `_migrate_v3` are written against src.db's pool-based `execute()`,
    `fetch_one()`, and `acquire()` helpers -- the real production path, not
    the per-connection style the rest of the sidecar's modules use for
    testability. Driving them for real would mean pointing src.db's global
    connection pool at the scratch DB, which:
      - requires network settings (DB_HOSTNAME etc.) the `make test`
        container never sets (only TEST_DB_URL is passed; settings.
        db_hostname defaults to "localhost", which is not reachable inside
        that container), and
      - issues real, non-rolled-back commits (execute()/fetch_one() are not
        wrapped in an explicit transaction), permanently mutating the
        shared scratch DB and breaking repeatability of `make test` without
        `make testdb` in between.

    Neither is acceptable, and the brief for this fix explicitly forbids
    changing _run_migrations/_migrate_v3's signature to work around it. So
    instead of changing what the code takes, this monkeypatches *where it
    looks*: `execute`, `fetch_one`, and `acquire` are names bound into
    `src.main`'s own module namespace by its `from src.db import ...` line,
    and are looked up there (not on `src.db`) every time `_run_migrations`
    etc. call them. Pointing those three names at the test's own `conn`
    means every statement the unmodified real migration path issues lands
    in -- and rolls back with -- the same transaction as the rest of the
    test. No function under test is altered in any way.
    """
    async def patched_execute(query, *args):
        return await conn.execute(query, *args)

    async def patched_fetch_one(query, *args):
        return await conn.fetchrow(query, *args)

    @asynccontextmanager
    async def patched_acquire():
        yield conn

    monkeypatch.setattr(main_module, "execute", patched_execute)
    monkeypatch.setattr(main_module, "fetch_one", patched_fetch_one)
    monkeypatch.setattr(main_module, "acquire", patched_acquire)


async def test_real_migration_path_drops_table_and_bumps_version(
    conn, route_main_db_calls_through_conn
):
    """Drive the real startup entry point (`_run_migrations`), not the
    extracted helper, so the version gate and the migration registration are
    proven wired up -- not just the DROP statement in isolation. A broken
    gate, a wrong comparison, or a migration that was never registered would
    leave `_face_sync_person_map` in place on a real upgrade; this fails if
    any of those regress.
    """
    # Simulate an existing v2 deployment: the table is present, and the
    # recorded schema version is one behind current.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_person_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        )
    """)
    await conn.execute(
        """
        INSERT INTO _face_sync_meta (key, value) VALUES ('schema_version', '2')
        ON CONFLICT (key) DO UPDATE SET value = '2'
        """
    )

    await main_module._run_migrations()

    table = await conn.fetchval("SELECT to_regclass('_face_sync_person_map')")
    assert table is None

    version = await conn.fetchval(
        "SELECT value FROM _face_sync_meta WHERE key = 'schema_version'"
    )
    assert version == str(main_module.SCHEMA_VERSION)


async def test_real_migration_path_is_a_no_op_once_already_current(
    conn, route_main_db_calls_through_conn
):
    """The version gate must not re-run a completed migration.

    A table named `_face_sync_person_map` existing at the current schema
    version is artificial -- by definition the real migration already
    dropped it by then -- but it is the cleanest way to observe that the
    gate short-circuits before ever calling `_migrate_v3` again: if it
    didn't, this table would vanish just like in the test above.
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS _face_sync_person_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        )
    """)
    await conn.execute(
        """
        INSERT INTO _face_sync_meta (key, value) VALUES ('schema_version', $1)
        ON CONFLICT (key) DO UPDATE SET value = $1
        """,
        str(main_module.SCHEMA_VERSION),
    )

    await main_module._run_migrations()

    table = await conn.fetchval("SELECT to_regclass('_face_sync_person_map')")
    assert table is not None


async def test_a_face_failure_keeps_the_batch_alive_and_defers_to_phase_2(conn, pair):
    """Phase 1's face copy must run under its own savepoint.

    sync_asset releases its savepoint before returning, so the face copy used
    to run bare inside the batch transaction: one failure aborted all 500
    assets in the batch and the cycle was lost. The asset itself is complete,
    so the guard keeps it and rewinds the mapping watermark instead -- Phase 2
    only looks at pairs where a source face is newer than synced_at, so
    without the rewind the asset would stay faceless forever.
    """
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt,
        source_asset_id=src_asset, target_asset_id=tgt_asset,
    )
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    # Fail the face INSERT the way a person_group vanishing under us would.
    await conn.execute(
        """
        CREATE FUNCTION _refuse_face() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'no faces for you'; END $$;
        CREATE TRIGGER _refuse_face_ins BEFORE INSERT ON asset_face
            FOR EACH ROW EXECUTE FUNCTION _refuse_face();
        """
    )

    assert await _sync_faces_guarded(conn, src_asset, tgt_asset, src, tgt) == 0

    # The batch transaction survives -- the whole point.
    assert await conn.fetchval("SELECT 1") == 1

    # And Phase 2 now picks the pair up, which it would not have done with the
    # watermark left at NOW().
    await conn.execute("DROP TRIGGER _refuse_face_ins ON asset_face")
    assert await sync_faces_incremental(conn) == 1
    assert await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    ) == pg


async def test_the_face_guard_is_transparent_on_success(conn, pair):
    """The savepoint must not swallow the happy path."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await _sync_faces_guarded(conn, src_asset, tgt_asset, src, tgt) == 1
    assert await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    ) == pg


async def test_v4_migration_strips_copied_embeddings_and_reclassifies(pair, 
    conn, route_main_db_calls_through_conn
):
    """An upgrade must retire the twins already in the database.

    Copies made before v4 carry a byte-identical clone of the source
    embedding, which votes a second time when recognition counts matches
    against minFaces. Fixing only new copies would leave every existing one
    distorting the next cluster-wide reset.
    """
    cg, src, tgt = pair
    src_asset, tgt_asset = await make_synced_pair(conn, src, tgt)

    embedding = "[" + ",".join(["0.1"] * 512) + "]"
    src_face = await make_face(conn, src_asset, bbox=(1, 2, 3, 4))
    tgt_face = await make_face(conn, tgt_asset, bbox=(1, 2, 3, 4))
    for fid in (src_face, tgt_face):
        await conn.execute(
            'INSERT INTO face_search ("faceId", embedding) VALUES ($1, $2)', fid, embedding
        )

    # An unrelated user's face must survive untouched -- the migration is
    # scoped to rows the sidecar created, via _face_sync_asset_map.
    other = await make_user(conn, cluster_group_id=cg)
    other_asset = await make_asset(conn, other)
    other_face = await make_face(conn, other_asset)
    await conn.execute(
        'INSERT INTO face_search ("faceId", embedding) VALUES ($1, $2)', other_face, embedding
    )

    await conn.execute(
        """
        INSERT INTO _face_sync_meta (key, value) VALUES ('schema_version', '3')
        ON CONFLICT (key) DO UPDATE SET value = '3'
        """
    )

    await main_module._run_migrations()

    # The copy loses its embedding and stops being a machine-learning face.
    assert await conn.fetchval(
        'SELECT count(*) FROM face_search WHERE "faceId" = $1', tgt_face
    ) == 0
    assert await conn.fetchval(
        'SELECT "sourceType"::text FROM asset_face WHERE id = $1', tgt_face
    ) == "manual"

    # The source and the unrelated user are left alone.
    assert await conn.fetchval(
        'SELECT count(*) FROM face_search WHERE "faceId" = $1', src_face
    ) == 1
    assert await conn.fetchval(
        'SELECT "sourceType"::text FROM asset_face WHERE id = $1', src_face
    ) == "machine-learning"
    assert await conn.fetchval(
        'SELECT count(*) FROM face_search WHERE "faceId" = $1', other_face
    ) == 1
    assert await conn.fetchval(
        'SELECT "sourceType"::text FROM asset_face WHERE id = $1', other_face
    ) == "machine-learning"

    assert await conn.fetchval(
        "SELECT value FROM _face_sync_meta WHERE key = 'schema_version'"
    ) == str(main_module.SCHEMA_VERSION)


async def test_v4_migration_is_idempotent(conn, pair):
    """Re-running finds nothing left to strip."""
    cg, src, tgt = pair
    src_asset, tgt_asset = await make_synced_pair(conn, src, tgt)
    tgt_face = await make_face(conn, tgt_asset)
    await conn.execute(
        'INSERT INTO face_search ("faceId", embedding) VALUES ($1, $2)',
        tgt_face, "[" + ",".join(["0.1"] * 512) + "]",
    )

    first = await main_module._strip_copied_face_embeddings(conn)
    second = await main_module._strip_copied_face_embeddings(conn)

    assert first == (1, 1)
    assert second == (0, 0)


async def test_phase_0_prunes_the_stale_mapping_before_phase_2_reads_it(conn, monkeypatch):
    """Ordering, not just presence: the prune must precede every map reader.

    Both placements pass a test that only checks the mapping is gone by the
    end of the cycle, which is why the original bug survived review. What
    matters is that nothing reads _face_sync_asset_map while a mapping points
    at a hard-deleted asset, so this records the order the phases actually run
    in and asserts the prune came first. Move cleanup_stale_mappings back to
    Phase 4 and this fails.

    The phase bodies are replaced with recorders because the ordering is the
    subject; each phase's own behaviour is tested elsewhere.
    """
    calls: list[str] = []

    @asynccontextmanager
    async def patched_transaction():
        yield conn

    def recorder(name, result=0):
        async def record(*args, **kwargs):
            calls.append(name)
            return result
        return record

    monkeypatch.setattr(sync_engine, "transaction", patched_transaction)
    monkeypatch.setattr(sync_engine.settings, "sync_jobs", [])
    monkeypatch.setattr(sync_engine, "cleanup_stale_mappings", recorder("phase0"))
    monkeypatch.setattr(sync_engine, "sync_faces_incremental", recorder("phase2"))
    monkeypatch.setattr(sync_engine, "sync_person_names", recorder("phase3_names"))
    monkeypatch.setattr(sync_engine, "sync_person_thumbnails", recorder("phase3_thumbs"))
    monkeypatch.setattr(sync_engine, "cleanup_deleted_assets", recorder("phase4_assets"))
    monkeypatch.setattr(sync_engine, "cleanup_reassigned_faces", recorder("phase4_faces"))
    monkeypatch.setattr(sync_engine, "cleanup_orphaned_persons", recorder("phase4_persons"))

    await sync_engine.run_full_sync()

    assert calls[0] == "phase0", f"the prune must run first, got {calls}"
    assert calls.index("phase0") < calls.index("phase2")
    assert calls.index("phase0") < calls.index("phase4_assets")


async def test_a_failed_phase_4_step_does_not_undo_the_deletions_before_it(conn, pair, monkeypatch):
    """Phase 4's steps share a transaction but must not share a fate.

    cleanup_deleted_assets unlinks thumbnails before deleting the rows that
    name them, and the filesystem does not roll back. Without a savepoint per
    step, a failure in cleanup_reassigned_faces restores asset rows whose
    files are already gone and Immich shows broken assets — indefinitely, if
    the failure is deterministic. The marker row here stands in for that
    committed-in-spirit work: it must survive the later failure.
    """
    cg, src, tgt = pair
    orphan = await make_asset(conn, tgt)
    await make_synced_pair(conn, src, tgt, target_asset_id=orphan)

    @asynccontextmanager
    async def patched_transaction():
        yield conn

    async def deletes_a_row(c):
        await c.execute("DELETE FROM _face_sync_asset_map WHERE target_asset_id = $1", orphan)
        return 1

    async def always_fails(c):
        # A real constraint violation, not a Python raise. A Python raise
        # leaves the transaction perfectly healthy, so a _cleanup_step with no
        # savepoint at all -- a bare try/except -- passes every assertion
        # below, including the closing SELECT 1. Only an aborted transaction
        # tells the two implementations apart.
        await c.execute(
            'INSERT INTO asset_face (id, "assetId") VALUES (gen_random_uuid(), NULL)'
        )

    monkeypatch.setattr(sync_engine, "transaction", patched_transaction)
    monkeypatch.setattr(sync_engine.settings, "sync_jobs", [])
    monkeypatch.setattr(sync_engine, "cleanup_stale_mappings", lambda c: _zero())
    monkeypatch.setattr(sync_engine, "sync_faces_incremental", lambda c: _zero())
    monkeypatch.setattr(sync_engine, "sync_person_names", lambda c: _zero())
    monkeypatch.setattr(sync_engine, "sync_person_thumbnails", lambda c: _zero())
    monkeypatch.setattr(sync_engine, "cleanup_deleted_assets", deletes_a_row)
    monkeypatch.setattr(sync_engine, "cleanup_reassigned_faces", always_fails)
    monkeypatch.setattr(sync_engine, "cleanup_orphaned_persons", lambda c: _zero())

    stats = await sync_engine.run_full_sync()

    # The cycle completed instead of raising, the failed step reported nothing,
    # and the earlier step's delete stands.
    assert stats["assets_cleaned"] == 1
    assert stats["faces_reassigned"] == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM _face_sync_asset_map WHERE target_asset_id = $1", orphan
    ) == 0
    # The transaction is still usable, which is the point of ROLLBACK TO.
    assert await conn.fetchval("SELECT 1") == 1


async def _zero():
    return 0


def _quiet_cycle(monkeypatch, conn):
    """Neutralise every phase and point them all at one connection.

    Returns nothing; callers re-patch whichever phase they are exercising.
    """
    @asynccontextmanager
    async def patched_transaction():
        yield conn

    monkeypatch.setattr(sync_engine, "transaction", patched_transaction)
    monkeypatch.setattr(sync_engine.settings, "sync_jobs", [])
    for name in (
        "cleanup_stale_mappings", "sync_faces_incremental", "sync_person_names",
        "sync_person_thumbnails", "cleanup_deleted_assets",
        "cleanup_reassigned_faces", "cleanup_orphaned_persons",
    ):
        monkeypatch.setattr(sync_engine, name, lambda c: _zero())


async def test_a_batch_that_syncs_nothing_stops_instead_of_spinning(conn, monkeypatch):
    """Phase 1's only termination condition when the batch stays full.

    sync_asset's catch-all returns None without recording anything, so a batch
    of persistently failing assets comes back identical every time. The short-
    batch exit never fires because the batch is never short. Without the
    no-progress break this loops forever inside a process whose only liveness
    signal is a health server that keeps answering 200.
    """
    _quiet_cycle(monkeypatch, conn)

    job = SimpleNamespace(
        name="stuck", source_user_id=None, target_user_id=None, album_id=None,
    )
    monkeypatch.setattr(sync_engine.settings, "sync_jobs", [job])

    # Collected, not asserted in place: an assert inside the coroutine raises
    # AssertionError, which _phase catches and logs like any other phase
    # failure, and the test passes while proving nothing.
    limits = []

    async def a_full_batch_of_doomed_assets(c, j, limit=None):
        limits.append(limit)
        # However many rows the caller asked for -- which is what makes the
        # batch "full" from the loop's point of view.
        return [{"id": None, "originalFileName": "x.jpg"}] * (limit or 500)

    monkeypatch.setattr(sync_engine, "get_unsynced_source_assets", a_full_batch_of_doomed_assets)
    monkeypatch.setattr(sync_engine, "validate_schema", lambda c: _zero())
    monkeypatch.setattr(
        sync_engine, "validate_cluster_group", lambda c, uids: _zero()
    )
    monkeypatch.setattr(sync_engine, "find_duplicate_filenames", lambda c, a, j: _empty_set())
    monkeypatch.setattr(sync_engine, "sync_asset", lambda c, s, j: _none())

    stats = await asyncio.wait_for(sync_engine.run_full_sync(), timeout=10)

    assert len(limits) == 1, f"the loop ran {len(limits)} times; it must break after one"
    assert stats["assets_synced"] == 0
    # The query must be asked for BATCH_SIZE rows. If the two ever disagree,
    # the short-batch exit decides the source is exhausted on a full batch and
    # the no-progress guard above becomes unreachable -- the loop still
    # terminates, so every other assertion here passes, which is why this one
    # is separate and explicit.
    assert limits == [sync_engine.BATCH_SIZE], (
        f"asked for {limits}, expected [{sync_engine.BATCH_SIZE}]"
    )


async def test_one_job_failing_does_not_cost_the_jobs_behind_it(conn, monkeypatch):
    """Phase 1 is guarded per job, not per phase."""
    _quiet_cycle(monkeypatch, conn)

    bad = SimpleNamespace(name="bad", source_user_id=None, target_user_id=None, album_id=None)
    good = SimpleNamespace(name="good", source_user_id=None, target_user_id=None, album_id=None)
    monkeypatch.setattr(sync_engine.settings, "sync_jobs", [bad, good])

    seen = []

    async def fails_for_the_first_job(c, j, limit=None):
        seen.append(j.name)
        if j.name == "bad":
            raise RuntimeError("this job's source library is unreadable")
        return []

    monkeypatch.setattr(sync_engine, "get_unsynced_source_assets", fails_for_the_first_job)

    await sync_engine.run_full_sync()

    assert seen == ["bad", "good"], f"the second job never ran: {seen}"


async def test_a_failed_phase_3_still_lets_phase_4_run(conn, monkeypatch):
    """The asymmetry that made this the Phase 0 bug one phase later.

    sync_person_thumbnails hardlinks files, so it can fail deterministically on
    a full disk or a permissions change. Unguarded, that exception left
    run_full_sync before Phase 4, so cleanup_deleted_assets never ran again --
    every cycle, for as long as the disk stayed full.
    """
    _quiet_cycle(monkeypatch, conn)

    ran = []

    async def disk_is_full(c):
        raise OSError(28, "No space left on device")

    async def records_that_it_ran(c):
        ran.append("phase4")
        return 0

    monkeypatch.setattr(sync_engine, "sync_person_thumbnails", disk_is_full)
    monkeypatch.setattr(sync_engine, "cleanup_deleted_assets", records_that_it_ran)

    stats = await sync_engine.run_full_sync()

    assert ran == ["phase4"], "Phase 4 was skipped by Phase 3's failure"
    assert stats["persons_updated"] == 0


async def test_a_schema_change_stops_the_cycle_rather_than_limping_on(conn, monkeypatch):
    """The one failure a phase guard must not swallow.

    Every later phase is more SQL against a shape we no longer understand.
    Continuing would turn one clear error into a burst of confusing ones.
    """
    _quiet_cycle(monkeypatch, conn)

    ran = []

    async def schema_moved(c):
        raise SchemaValidationError("asset_face.personId is gone")

    monkeypatch.setattr(sync_engine, "sync_faces_incremental", schema_moved)
    monkeypatch.setattr(
        sync_engine, "cleanup_deleted_assets", lambda c: _record_and_zero(ran)
    )

    with pytest.raises(SchemaValidationError):
        await sync_engine.run_full_sync()

    assert ran == [], "the cycle carried on past a schema change"


async def test_a_broken_connection_reaches_the_sync_loop(conn, monkeypatch):
    """sync_loop resets the pool on one, so a phase guard must not eat it.

    Swallowed, it would leave every later phase failing against a dead
    connection and the pool never rebuilt.
    """
    _quiet_cycle(monkeypatch, conn)

    async def connection_died(c):
        raise asyncpg.exceptions.ConnectionDoesNotExistError("connection is closed")

    monkeypatch.setattr(sync_engine, "sync_person_names", connection_died)

    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await sync_engine.run_full_sync()


async def _empty_set():
    return set()


async def _none():
    return None


async def _record_and_zero(into):
    into.append("ran")
    return 0
