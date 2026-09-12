"""dedup_synced.py's delete path.

This is the tool that removes synced assets duplicating the target user's own
uploads. Its per-item unit is two halves -- delete the asset, record the source
as skipped so the sync engine will not recreate it -- and the second half was
broken in a way no test could see, because nothing imported this module.

The failure was worse than the "N failed" it reported. delete_target_asset
unlinks thumbnails before deleting the rows that name them, so the disk change
had already happened when the bad INSERT aborted the surrounding batch
transaction: every asset row came back on rollback, and their files did not.
"""
import dedup_synced as dedup_mod
import src.asset_sync as asset_sync_mod
from src.db import transaction
from tests.conftest import make_asset, make_synced_pair


async def test_skip_record_names_the_target_user(conn, pair):
    """_face_sync_skipped is keyed on (source_asset_id, target_user_id).

    The INSERT this replaces named only source_asset_id, so it violated a NOT
    NULL and targeted a unique index that does not exist. Every delete failed.
    """
    cg, src, tgt = pair
    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt, source_asset_id=src_asset, target_asset_id=tgt_asset,
    )

    ok = await dedup_mod.delete_synced_asset(conn, src_asset, tgt_asset, tgt)

    assert ok is True
    row = await conn.fetchrow(
        "SELECT target_user_id, reason FROM _face_sync_skipped WHERE source_asset_id = $1",
        src_asset,
    )
    assert row is not None, "source was not recorded as skipped"
    assert row["target_user_id"] == tgt
    assert row["reason"] == "duplicate_filename"
    gone = await conn.fetchval("SELECT count(*) FROM asset WHERE id = $1", tgt_asset)
    assert gone == 0


async def test_the_same_source_can_be_skipped_for_two_targets(conn):
    """The composite key is the point: one source asset, two target users.

    ON CONFLICT (source_asset_id) alone would have collapsed these into one
    row even if the column list had been right.
    """
    from tests.conftest import make_cluster_group, make_user

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt_a = await make_user(conn, cluster_group_id=cg)
    tgt_b = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    for tgt in (tgt_a, tgt_b):
        tgt_asset = await make_asset(conn, tgt)
        await make_synced_pair(
            conn, src, tgt, source_asset_id=src_asset, target_asset_id=tgt_asset,
        )
        assert await dedup_mod.delete_synced_asset(conn, src_asset, tgt_asset, tgt)

    skipped = await conn.fetchval(
        "SELECT count(*) FROM _face_sync_skipped WHERE source_asset_id = $1", src_asset,
    )
    assert skipped == 2


async def test_a_repeated_delete_is_idempotent(conn, pair):
    """Re-running the tool over a source already recorded must not fail."""
    cg, src, tgt = pair
    src_asset = await make_asset(conn, src)

    for _ in range(2):
        tgt_asset = await make_asset(conn, tgt)
        await make_synced_pair(
            conn, src, tgt, source_asset_id=src_asset, target_asset_id=tgt_asset,
        )
        assert await dedup_mod.delete_synced_asset(conn, src_asset, tgt_asset, tgt)


async def test_a_failed_skip_record_does_not_abort_the_batch(conn, pair, monkeypatch):
    """The savepoint, which is the half that turned a bad row into lost files.

    Without it the failed INSERT leaves the batch transaction aborted: every
    later delete in the batch raises on its own SAVEPOINT, the commit silently
    becomes a rollback, and the user is told "N failed" while the thumbnails
    for all N are already gone from disk.

    The failure is injected as real SQL rather than a Python raise, because a
    Python raise leaves the transaction perfectly healthy and a savepoint-less
    implementation would pass this test.
    """
    cg, src, tgt = pair
    doomed_src = await make_asset(conn, src)
    doomed_tgt = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt, source_asset_id=doomed_src, target_asset_id=doomed_tgt,
    )

    async def explode(c, source_asset_ids, target_user_id):
        # NOT NULL on reason: aborts the transaction exactly as the old
        # INSERT's missing target_user_id did.
        await c.execute(
            "INSERT INTO _face_sync_skipped (source_asset_id, target_user_id, reason) "
            "VALUES ($1, $2, NULL)",
            list(source_asset_ids)[0], target_user_id,
        )

    monkeypatch.setattr(dedup_mod, "record_skipped_duplicates", explode)

    assert await dedup_mod.delete_synced_asset(conn, doomed_src, doomed_tgt, tgt) is False

    # The connection is still usable, which is what the savepoint buys.
    monkeypatch.setattr(
        dedup_mod, "record_skipped_duplicates", asset_sync_mod.record_skipped_duplicates,
    )
    next_src = await make_asset(conn, src)
    next_tgt = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt, source_asset_id=next_src, target_asset_id=next_tgt,
    )

    assert await dedup_mod.delete_synced_asset(conn, next_src, next_tgt, tgt) is True
    assert await conn.fetchval("SELECT count(*) FROM asset WHERE id = $1", next_tgt) == 0


def test_importing_the_module_does_not_exit():
    """It used to sys.exit(1) at import when .env was absent, which is why the
    broken INSERT above went four commits without a test. The check moved into
    main(), where it belongs."""
    assert callable(dedup_mod.delete_synced_asset)
    assert dedup_mod.record_skipped_duplicates is asset_sync_mod.record_skipped_duplicates
