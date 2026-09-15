"""Pruning the person groups that only exist because synced faces voted twice.

The judgement is on *original* faces -- a face whose asset is a target in
_face_sync_asset_map is a copy and does not count -- so these tests always set
up both halves of a synced pair and check the copy is discounted.
"""
import prune_inflated_people as prune_mod
from prune_inflated_people import preview, prune
from tests.conftest import (
    make_asset, make_cluster_group, make_face, make_person, make_person_group,
    make_synced_pair, make_user,
)


async def _synced_group(conn, pair, *, originals: int, name: str = ""):
    """A person group with `originals` real faces, each mirrored onto a copy."""
    cg, src, tgt = pair
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


async def test_group_below_min_faces_on_originals_is_pruned(conn, pair):
    """Two real sightings plus their two copies looks like 4 faces to Immich,
    which is what let it clear a threshold of 3. Judged on originals it is 2.
    """
    pg = await _synced_group(conn, pair, originals=2)

    stats = await preview(conn, 3)
    assert stats["groups"] == 1
    assert stats["original_faces"] == 2
    assert stats["faces_unassigned"] == 4  # 2 originals + 2 copies

    assert await prune(conn, 3) == 1
    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 0


async def test_group_with_enough_originals_is_kept(conn, pair):
    pg = await _synced_group(conn, pair, originals=3)

    assert (await preview(conn, 3))["groups"] == 0
    assert await prune(conn, 3) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 1


async def test_named_group_is_never_pruned(conn, pair):
    """A name means a human judged the person real, however it was clustered."""
    pg = await _synced_group(conn, pair, originals=1, name="Granny")

    assert (await preview(conn, 3))["groups"] == 0
    assert await prune(conn, 3) == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 1


async def test_pruning_unassigns_the_faces_rather_than_deleting_them(conn, pair):
    """The photos and their face boxes must survive -- only the attribution goes."""
    pg = await _synced_group(conn, pair, originals=2)
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


async def test_preview_changes_nothing(conn, pair):
    """The dry-run path must be side-effect free."""
    pg = await _synced_group(conn, pair, originals=1)

    await preview(conn, 3)
    await preview(conn, 3)

    assert await conn.fetchval(
        "SELECT count(*) FROM person_group WHERE id = $1", pg
    ) == 1
    assert await conn.fetchval(
        'SELECT count(*) FROM person WHERE "personGroupId" = $1', pg
    ) == 2


async def test_a_group_of_only_copies_is_pruned(conn, pair):
    """Zero originals -- the group exists purely because copies were counted."""
    cg, src, tgt = pair
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


async def test_a_small_group_holding_no_copy_is_left_alone(conn, pair):
    """Two original faces, below minFaces, and not a copy among them.

    The count is deliberately 2, not 3: at 3 the group is kept for the
    ordinary reason and the test proves nothing. Below the threshold it is
    only spared by the "must hold a copy" guard, which is the point. This
    covers both a user the sidecar never touched and a legitimate person that
    has since shrunk — Immich's minFaces gates cluster creation, not
    persistence, so unassigning faces in the UI makes one of these.
    """
    cg = await make_cluster_group(conn)
    owner = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, owner, pg)
    for i in range(2):
        asset = await make_asset(conn, owner)
        await make_face(conn, asset, person_group_id=pg, bbox=(i, i, i + 5, i + 5))

    assert (await preview(conn, 3))["groups"] == 0
    assert await prune(conn, 3) == 0


def test_the_warning_fires_on_a_stock_instance():
    """configured_min_faces returns None when the admin never overrode minFaces.

    Gating the warning on "configured is not None" meant it never fired in the
    common case, which is the case where --min-faces 10 --apply silently
    deletes every copy-holding group under ten.
    """
    lines = prune_mod.min_faces_warning(10, None)

    assert lines, "no warning on a stock instance"
    assert "ABOVE" in lines[0]
    assert "10" in lines[0] and "3" in lines[0]


def test_the_warning_reads_the_direction():
    """Below the effective setting is incomplete; above it is dangerous.

    The message said "Above your instance's setting" for both, which is wrong
    half the time in the one sentence meant to tell you which way you erred.
    """
    above = prune_mod.min_faces_warning(6, 3)
    below = prune_mod.min_faces_warning(2, 3)

    assert "ABOVE" in above[0] and "WARNING" in above[0]
    assert "below" in below[0] and "NOTE" in below[0]
    assert "ABOVE" not in below[0]


def test_no_warning_when_the_value_matches():
    assert prune_mod.min_faces_warning(3, None) == []
    assert prune_mod.min_faces_warning(7, 7) == []


def test_the_cli_default_is_immich_s_default():
    """A default of 3 hardcoded in two places is one rename from disagreeing."""
    assert prune_mod.IMMICH_DEFAULT_MIN_FACES == 3
    parser = prune_mod.build_parser()
    assert parser.parse_args([]).min_faces == prune_mod.IMMICH_DEFAULT_MIN_FACES
