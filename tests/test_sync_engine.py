"""End-to-end face-sync convergence, and proof that _face_sync_person_map is gone.

v3.2.0 cluster-group port: person identity is shared via Immich's person_group
table, so the sidecar no longer maps source persons to target persons. Tasks
3-5 removed every read/write of `_face_sync_person_map`; this task retires
the table itself.
"""
from contextlib import asynccontextmanager

import pytest

from src import main as main_module
from src.cleanup import cleanup_reassigned_faces
from src.main import _drop_person_map_table
from src.ml_sync import sync_faces_for_asset, sync_faces_incremental
from src.person_sync import cleanup_orphaned_persons, sync_person_names
from src.sync_engine import _sync_faces_guarded
from tests.conftest import (
    make_asset, make_cluster_group, make_face, make_person, make_person_group,
    make_synced_pair, make_user,
)


async def test_full_face_flow_converges(conn):
    """Sync a face, rename at source, reassign at source -- all must converge."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
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
    vacuous either way: the fixture (dumped from a live instance) still
    creates the table, so a fresh `make testdb` always has it, and a
    same-session re-run would find it already dropped by an earlier test.
    Neither state proves anything about the sidecar's own code.

    Instead this exercises the real drop statement (`src.main.
    _drop_person_map_table`) directly against the per-test transactional
    connection, and checks the *transition*: the table is present
    beforehand (so a no-op implementation would fail this test) and gone
    afterward. The transaction rolls back at the end of the test, so the
    shared scratch DB is untouched for the next test.
    """
    before = await conn.fetchval("SELECT to_regclass('_face_sync_person_map')")
    assert before is not None, "fixture no longer seeds the table -- update this test"

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


async def test_a_face_failure_keeps_the_batch_alive_and_defers_to_phase_2(conn):
    """Phase 1's face copy must run under its own savepoint.

    sync_asset releases its savepoint before returning, so the face copy used
    to run bare inside the batch transaction: one failure aborted all 500
    assets in the batch and the cycle was lost. The asset itself is complete,
    so the guard keeps it and rewinds the mapping watermark instead -- Phase 2
    only looks at pairs where a source face is newer than synced_at, so
    without the rewind the asset would stay faceless forever.
    """
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
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


async def test_the_face_guard_is_transparent_on_success(conn):
    """The savepoint must not swallow the happy path."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await _sync_faces_guarded(conn, src_asset, tgt_asset, src, tgt) == 1
    assert await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    ) == pg
