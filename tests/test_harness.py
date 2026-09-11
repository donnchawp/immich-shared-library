"""Smoke tests for the test harness itself.

These assert the scratch database really is on the v3.2.0 cluster-group schema.
If they fail, every other test in this suite is testing the wrong thing.
"""
from tests.conftest import (
    make_asset,
    make_cluster_group,
    make_face,
    make_person,
    make_person_group,
    make_user,
)


async def test_schema_is_v3_2_0(conn):
    """person.id is gone and asset_face.personGroupId exists."""
    person_id_col = await conn.fetchval(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'person' AND column_name = 'id'"
    )
    assert person_id_col is None

    face_col = await conn.fetchval(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'asset_face' AND column_name = 'personGroupId'"
    )
    assert face_col == "personGroupId"


async def test_two_users_can_share_one_person_group(conn):
    """The central premise: one person_group, two person rows, two owners."""
    cg = await make_cluster_group(conn)
    alice = await make_user(conn, cluster_group_id=cg)
    bob = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    await make_person(conn, alice, pg, name="Mum")
    await make_person(conn, bob, pg, name="Mam")

    names = await conn.fetch(
        'SELECT "ownerId", name FROM person WHERE "personGroupId" = $1 ORDER BY name', pg
    )
    assert [r["name"] for r in names] == ["Mam", "Mum"]


async def test_faces_from_both_users_share_the_group(conn):
    cg = await make_cluster_group(conn)
    alice = await make_user(conn, cluster_group_id=cg)
    bob = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    a_asset = await make_asset(conn, alice)
    b_asset = await make_asset(conn, bob)
    await make_face(conn, a_asset, person_group_id=pg)
    await make_face(conn, b_asset, person_group_id=pg)

    count = await conn.fetchval(
        'SELECT COUNT(*) FROM asset_face WHERE "personGroupId" = $1', pg
    )
    assert count == 2


async def test_rollback_leaves_no_trace(conn):
    """The conn fixture must roll back, or tests will pollute each other."""
    before = await conn.fetchval("SELECT COUNT(*) FROM cluster_group")
    await make_cluster_group(conn)
    after = await conn.fetchval("SELECT COUNT(*) FROM cluster_group")
    assert after == before + 1
    # The rollback itself is asserted by test_scratch_db_is_empty below.


async def test_scratch_db_is_empty(conn):
    """No test has committed anything into the scratch database."""
    for table in ("cluster_group", "person_group", "person", "asset", "asset_face"):
        count = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
        assert count == 0, f"{table} is not empty - a test committed data"
