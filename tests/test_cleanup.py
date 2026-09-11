from src.cleanup import cleanup_reassigned_faces
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)


async def _link(conn, src_asset, tgt_asset, src_user, tgt_user):
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        src_asset, tgt_asset, src_user, tgt_user,
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

    updated = await cleanup_reassigned_faces(conn)

    assert updated == 1
    now = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert now == new_pg


async def test_is_a_no_op_when_already_in_sync(conn):
    """Must converge — this is what bug 851005b was about."""
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
