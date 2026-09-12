"""Pruning the person groups that only exist because synced faces voted twice.

The judgement is on *original* faces -- a face whose asset is a target in
_face_sync_asset_map is a copy and does not count -- so these tests always set
up both halves of a synced pair and check the copy is discounted.
"""
from prune_inflated_people import preview, prune
from tests.conftest import (
    make_asset, make_cluster_group, make_face, make_person, make_person_group,
    make_synced_pair, make_user,
)


async def _synced_group(conn, *, originals: int, name: str = ""):
    """A person group with `originals` real faces, each mirrored onto a copy."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name=name)
    await make_person(conn, tgt, pg)

    for i in range(originals):
        src_asset = await make_asset(conn, src)
        tgt_asset = await make_asset(conn, tgt)
        await make_synced_pair(
            conn, src, tgt, source_asset_id=src_asset, target_asset_id=tgt_asset,
        )
        await make_face(conn, src_asset, person_group_id=pg, bbox=(i, i, i + 5, i + 5))
        await make_face(conn, tgt_asset, person_group_id=pg, bbox=(i, i, i + 5, i + 5))
    return pg


async def test_group_below_min_faces_on_originals_is_pruned(conn):
    """Two real sightings plus their two copies looks like 4 faces to Immich,
    which is what let it clear a threshold of 3. Judged on originals it is 2.
    """
    pg = await _synced_group(conn, originals=2)

    stats = await preview(conn, 3)
    assert stats["groups"] == 1
    assert stats["original_faces"] == 2
    assert stats["faces_unassigned"] == 4  # 2 originals + 2 copies

    assert await prune(conn, 3) == 1
    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 0


async def test_group_with_enough_originals_is_kept(conn):
    pg = await _synced_group(conn, originals=3)

    assert (await preview(conn, 3))["groups"] == 0
    assert await prune(conn, 3) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 1


async def test_named_group_is_never_pruned(conn):
    """A name means a human judged the person real, however it was clustered."""
    pg = await _synced_group(conn, originals=1, name="Granny")

    assert (await preview(conn, 3))["groups"] == 0
    assert await prune(conn, 3) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 1


async def test_pruning_unassigns_the_faces_rather_than_deleting_them(conn):
    """The photos and their face boxes must survive -- only the attribution goes."""
    pg = await _synced_group(conn, originals=2)
    before = await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "personGroupId" = $1', pg
    )
    assert before == 4

    await prune(conn, 3)

    assert await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "personGroupId" = $1', pg
    ) == 0
    # The face rows themselves are untouched, just unassigned.
    assert await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "personGroupId" IS NULL'
    ) >= 4


async def test_preview_changes_nothing(conn):
    """The dry-run path must be side-effect free."""
    pg = await _synced_group(conn, originals=1)

    await preview(conn, 3)
    await preview(conn, 3)

    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 1
    assert await conn.fetchval(
        'SELECT count(*) FROM person WHERE "personGroupId" = $1', pg
    ) == 2


async def test_a_group_of_only_copies_is_pruned(conn):
    """Zero originals -- the group exists purely because copies were counted."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt, source_asset_id=src_asset, target_asset_id=tgt_asset,
    )
    await make_face(conn, tgt_asset, person_group_id=pg)

    assert (await preview(conn, 3))["groups"] == 1
    assert await prune(conn, 3) == 1


async def test_unsynced_users_are_judged_on_all_their_faces(conn):
    """A user the sidecar never touched has no copies, so nothing is discounted
    and a legitimate small group of theirs is left alone.
    """
    cg = await make_cluster_group(conn)
    owner = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, owner, pg)
    for i in range(3):
        asset = await make_asset(conn, owner)
        await make_face(conn, asset, person_group_id=pg, bbox=(i, i, i + 5, i + 5))

    assert (await preview(conn, 3))["groups"] == 0
    assert await prune(conn, 3) == 0
