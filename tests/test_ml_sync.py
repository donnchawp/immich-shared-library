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

    await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    face_asset_id = await conn.fetchval(
        'SELECT "faceAssetId" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        tgt, pg,
    )
    target_face_id = await conn.fetchval(
        'SELECT id FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert face_asset_id == target_face_id
