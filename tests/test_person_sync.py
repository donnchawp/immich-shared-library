from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)
from src.person_sync import cleanup_orphaned_persons, ensure_target_person, sync_person_names


async def test_creates_target_person_row_for_shared_group(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")

    result = await ensure_target_person(conn, pg, src, tgt)

    assert result == pg
    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Granny"


async def test_is_idempotent(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")

    await ensure_target_person(conn, pg, src, tgt)
    await ensure_target_person(conn, pg, src, tgt)

    count = await conn.fetchval(
        'SELECT COUNT(*) FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert count == 1


async def test_does_not_overwrite_a_name_the_target_already_set(conn):
    """The target user's own naming wins — we only fill an empty name."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")
    await make_person(conn, tgt, pg, name="Nana")

    await ensure_target_person(conn, pg, src, tgt)
    await sync_person_names(conn)

    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Nana"


async def test_returns_none_when_source_has_no_person_row(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)  # no person row for src

    assert await ensure_target_person(conn, pg, src, tgt) is None


async def _map_synced_pair(conn, src, tgt):
    """Give (src, tgt) a _face_sync_asset_map row, as Phase 1 would after
    syncing at least one asset — this is what puts a pair in scope for
    cleanup_orphaned_persons."""
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


async def test_cleanup_removes_target_person_with_no_faces(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)  # target person; source has none; no faces
    await _map_synced_pair(conn, src, tgt)

    removed = await cleanup_orphaned_persons(conn)

    assert removed == 1


async def test_cleanup_keeps_target_person_that_still_has_faces(conn):
    """Deleting a person whose faces remain would null those faces."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)
    asset = await make_asset(conn, tgt)
    await make_face(conn, asset, person_group_id=pg)
    await _map_synced_pair(conn, src, tgt)

    removed = await cleanup_orphaned_persons(conn)

    assert removed == 0


async def test_cleanup_ignores_persons_of_unmanaged_users(conn):
    """A user the sidecar never syncs must not have persons deleted."""
    cg = await make_cluster_group(conn)
    stranger = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, stranger, pg, name="Not ours")  # no faces, no map row

    assert await cleanup_orphaned_persons(conn) == 0
    survived = await conn.fetchval(
        'SELECT count(*) FROM person WHERE "ownerId" = $1', stranger
    )
    assert survived == 1
