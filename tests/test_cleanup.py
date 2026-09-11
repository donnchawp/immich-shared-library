from uuid import uuid4

from src.cleanup import cleanup_deleted_assets, cleanup_reassigned_faces
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group,
    make_synced_pair, make_user,
)


async def _link(conn, src_asset, tgt_asset, src_user, tgt_user):
    """Map two assets this file already made. See conftest.make_synced_pair."""
    await make_synced_pair(
        conn, src_user, tgt_user,
        source_asset_id=src_asset, target_asset_id=tgt_asset,
    )


async def test_propagates_a_source_reassignment(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    old_pg = await make_person_group(conn, cg)
    new_pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)

    # Source face now points at new_pg; target still has old_pg
    await make_face(conn, src_asset, person_group_id=new_pg, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=old_pg, bbox=(1, 1, 9, 9))
    # Phase 2 (ensure_target_person) normally creates this; the reassignment is
    # gated on it, so a face is never pointed at a group the target has no row on.
    await make_person(conn, tgt, new_pg)

    updated = await cleanup_reassigned_faces(conn)

    assert updated == 1
    now = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert now == new_pg

    # Convergence: running the same cycle again after a real write must do
    # nothing — regression guard for bug 851005b (non-convergent reassignment
    # loop that kept re-flagging the same faces forever).
    assert await cleanup_reassigned_faces(conn) == 0


async def test_is_a_no_op_when_already_in_sync(conn):
    """A no-op stays a no-op. Weaker than the convergence guard in
    test_propagates_a_source_reassignment (which proves a real write does not
    get re-flagged on the next cycle — the actual shape of bug 851005b); this
    only covers the case where there was never anything to reconcile."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await cleanup_reassigned_faces(conn) == 0
    assert await cleanup_reassigned_faces(conn) == 0


async def test_propagates_an_unassignment(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)
    await make_face(conn, src_asset, person_group_id=None, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await cleanup_reassigned_faces(conn) == 1
    now = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert now is None


async def test_does_not_assign_a_group_the_target_has_no_person_row_on(conn):
    """Phase 4 must not point a target face at a group the target user has no
    ``person`` row on: with no row, Immich's deleteEmptyGroups can drop the
    group and null the face. Phase 2 creates that row, but it runs in its own
    transaction and may have failed, so Phase 4 states the precondition itself.
    """
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    old_pg = await make_person_group(conn, cg)
    new_pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)
    await make_face(conn, src_asset, person_group_id=new_pg, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=old_pg, bbox=(1, 1, 9, 9))
    # Deliberately no person row for tgt on new_pg. The *source* has one, to
    # prove the gate checks the target's ownership and not merely existence.
    await make_person(conn, src, new_pg)

    assert await cleanup_reassigned_faces(conn) == 0
    still = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert still == old_pg


async def test_one_undeletable_asset_does_not_poison_the_cleanup_transaction(conn):
    """A per-asset failure must roll back to a savepoint, not just be logged.

    Catching the exception without ``ROLLBACK TO SAVEPOINT`` leaves Postgres in
    an aborted transaction: every later iteration fails with
    InFailedSQLTransactionError, each logged as its own "failure", and the
    whole cleanup unwinds. This is the bug c263e97 fixed in delete_synced.py;
    the production path had the same shape.
    """
    src = await make_user(conn)
    tgt = await make_user(conn)

    # Two mappings whose source assets no longer exist -- both are orphans.
    doomed = await make_asset(conn, tgt)
    good = await make_asset(conn, tgt)
    await _link(conn, uuid4(), doomed, src, tgt)
    await _link(conn, uuid4(), good, src, tgt)

    # Make one target asset undeletable, the way a NO ACTION foreign key from a
    # table the sidecar does not know about would.
    await conn.execute(
        'UPDATE asset SET "originalFileName" = $1 WHERE id = $2', "boom.jpg", doomed
    )
    await conn.execute(
        """
        CREATE FUNCTION _refuse_delete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'refusing to delete %', OLD.id; END $$;
        CREATE TRIGGER _refuse_delete_boom BEFORE DELETE ON asset
            FOR EACH ROW WHEN (OLD."originalFileName" = 'boom.jpg')
            EXECUTE FUNCTION _refuse_delete();
        """
    )

    count = await cleanup_deleted_assets(conn)

    # The transaction must still be usable. Order-independent: whichever asset
    # the LEFT JOIN returns first, an unrolled-back failure poisons everything
    # after it and this query raises.
    assert await conn.fetchval("SELECT 1") == 1

    assert count == 1
    assert await conn.fetchval("SELECT 1 FROM asset WHERE id = $1", good) is None
    assert await conn.fetchval("SELECT 1 FROM asset WHERE id = $1", doomed) == 1
    # The doomed mapping survives so the next cycle retries it.
    assert await conn.fetchval(
        "SELECT count(*) FROM _face_sync_asset_map WHERE target_asset_id = $1", doomed
    ) == 1
