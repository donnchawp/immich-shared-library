from src.ml_sync import sync_faces_for_asset
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)


async def test_copies_person_group_id_verbatim(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 2, 3, 4))

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 1
    copied = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied == pg


async def test_creates_the_target_person_row(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg)

    await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Dad"


async def test_unassigned_face_copies_with_null_group(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=None)

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 1
    copied = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied is None


async def test_does_not_duplicate_an_existing_bounding_box(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, bbox=(5, 5, 15, 15))
    await make_face(conn, tgt_asset, bbox=(5, 5, 15, 15))  # already there

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 0
    total = await conn.fetchval(
        'SELECT COUNT(*) FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert total == 1


async def test_sets_face_asset_id_on_the_target_person(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg)

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)
    assert count == 1

    target_face_id = await conn.fetchval(
        'SELECT id FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert target_face_id is not None, "no face was copied to the target asset"

    face_asset_id = await conn.fetchval(
        'SELECT "faceAssetId" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        tgt, pg,
    )
    assert face_asset_id == target_face_id


async def test_incremental_sync_skips_mappings_whose_target_asset_is_gone(conn):
    """A hard-deleted target asset must not crash the whole cycle.

    Phase 2 reads pairs straight out of _face_sync_asset_map. When the target
    user hard-deletes a synced asset the mapping row survives until Phase 4's
    cleanup_stale_mappings prunes it — but Phase 4 runs *after* Phase 2, so an
    unguarded Phase 2 raises ForeignKeyViolationError, aborts the run, and the
    cleanup that would have fixed it never executes. That deadlocks the sidecar
    on every subsequent cycle.
    """
    from src.ml_sync import sync_faces_incremental
    from uuid import uuid4

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    missing_target = uuid4()  # never inserted into asset

    # synced_at in the past so the source face counts as updated since
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW() - INTERVAL '1 day')
        """,
        src_asset, missing_target, src, tgt,
    )
    await make_face(conn, src_asset, bbox=(1, 1, 9, 9))

    # Must not raise; the stale pair is simply skipped for Phase 4 to prune.
    assert await sync_faces_incremental(conn) == 0


async def test_incremental_sync_still_syncs_live_pairs(conn):
    """The guard must not stop real work — pairs with a live target still sync."""
    from src.ml_sync import sync_faces_incremental

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW() - INTERVAL '1 day')
        """,
        src_asset, tgt_asset, src, tgt,
    )
    await make_face(conn, src_asset, bbox=(2, 2, 8, 8))

    assert await sync_faces_incremental(conn) == 1
    copied = await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied == 1
