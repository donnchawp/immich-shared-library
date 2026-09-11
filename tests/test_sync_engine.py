"""End-to-end face-sync convergence, and proof that _face_sync_person_map is gone.

v3.2.0 cluster-group port: person identity is shared via Immich's person_group
table, so the sidecar no longer maps source persons to target persons. Tasks
3-5 removed every read/write of `_face_sync_person_map`; this task retires
the table itself.
"""
from src.cleanup import cleanup_reassigned_faces
from src.main import _drop_person_map_table
from src.ml_sync import sync_faces_for_asset
from src.person_sync import cleanup_orphaned_persons, sync_person_names
from tests.conftest import (
    make_asset, make_cluster_group, make_face, make_person, make_person_group, make_user,
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
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        src_asset, tgt_asset, src, tgt,
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
