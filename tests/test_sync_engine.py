"""End-to-end face-sync convergence, and proof that _face_sync_person_map is gone.

v3.2.0 cluster-group port: person identity is shared via Immich's person_group
table, so the sidecar no longer maps source persons to target persons. Tasks
3-5 removed every read/write of `_face_sync_person_map`; this task retires
the table itself.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from src import main as main_module
from src.asset_sync import sync_asset
from src.cleanup import cleanup_reassigned_faces, delete_target_asset
from src.main import _drop_person_map_table, _is_connection_error
from src.ml_sync import sync_faces_for_asset, sync_faces_for_asset_guarded, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names
from src import sync_engine
from src.sync_engine import _cleanup_step, _sync_faces_guarded
from tests.conftest import (
    TEST_DB_URL, make_asset, make_face, make_person, make_person_group,
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
        raise RuntimeError("Immich dropped the group between our read and our write")

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


async def _select_one(c):
    return await c.fetchval("SELECT 1")


_GUARDS = {
    "cleanup_step": lambda c: _cleanup_step(c, _select_one),
    "face_guard": lambda c: sync_faces_for_asset_guarded(c, uuid4(), uuid4(), uuid4(), uuid4()),
    "sync_asset": lambda c: sync_asset(
        c, {"id": uuid4(), "originalPath": "/nowhere"},
        SimpleNamespace(target_user_id=uuid4(), target_library_id=uuid4()),
    ),
    "delete_target_asset": lambda c: delete_target_asset(c, uuid4()),
}


@pytest.mark.parametrize("guard", _GUARDS.values(), ids=_GUARDS.keys())
async def test_a_savepoint_guard_does_not_swallow_a_lost_connection(conn, guard):
    """A dropped connection must reach sync_loop, not be logged and absorbed.

    Restarting Immich's Postgres under a running sidecar killed the connection
    mid-way through Phase 4. _cleanup_step caught it like any failed statement,
    logged "the rest of the cycle stands" for a cycle that had committed
    nothing, and ran the next step into "connection has been released back to
    the pool". Every per-item savepoint guard has the same shape, so each is
    checked: killed underneath, it must raise something sync_loop recognizes
    as a connection error, so the pool gets reset.

    The victim is a connection of its own; the fixture's `conn` does the
    killing, because its teardown needs it alive to roll back.
    """
    victim = await asyncpg.connect(TEST_DB_URL)
    try:
        pid = await victim.fetchval("SELECT pg_backend_pid()")
        await conn.execute("SELECT pg_terminate_backend($1)", pid)

        with pytest.raises(Exception) as caught:
            await guard(victim)
    finally:
        victim.terminate()

    assert _is_connection_error(caught.value), repr(caught.value)
