"""Sidecar (XMP) asset_file handling.

Immich records a Lightroom XMP as a third `asset_file` row of type 'sidecar',
whose path sits next to the original in the external library — NOT under the
upload location like thumbnails and previews.

Two things follow, and both have bitten:

1. It must not be hardlinked. The target's external-library directory is a
   symlink to the source's, so the file is already visible at the target path.
   Trying to hardlink it fails the upload-directory check and the row is lost.

2. It must never be handed to remove_hardlinks. Deleting the target's XMP path
   resolves through that symlink to the SOURCE's own file.
"""
from uuid import uuid4

from src.config import SyncJob
from tests.conftest import make_asset, make_cluster_group, make_user

SRC_PREFIX = "/external_library/donncha/shared/"
TGT_PREFIX = "/external_library/tester/donncha/"


def _job(source_user_id, target_user_id):
    return SyncJob(
        name="test",
        source_user_id=source_user_id,
        target_user_id=target_user_id,
        target_library_id=uuid4(),
        source_path_prefix=SRC_PREFIX,
        target_path_prefix=TGT_PREFIX,
    )


async def _add_file(conn, asset_id, file_type, path):
    await conn.execute(
        'INSERT INTO asset_file (id, "assetId", type, path) VALUES ($1, $2, $3, $4)',
        uuid4(), asset_id, file_type, path,
    )


async def test_sidecar_row_is_created_with_a_remapped_path(conn):
    from src.asset_sync import _sync_asset_files

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    src_asset = await make_asset(conn, src, original_path=f"{SRC_PREFIX}p.jpg")
    tgt_asset = await make_asset(conn, tgt, original_path=f"{TGT_PREFIX}p.jpg")
    await _add_file(conn, src_asset, "sidecar", f"{SRC_PREFIX}p.jpg.xmp")

    await _sync_asset_files(conn, src_asset, tgt_asset, src, tgt, _job(src, tgt))

    path = await conn.fetchval(
        'SELECT path FROM asset_file WHERE "assetId" = $1 AND type = $2',
        tgt_asset, "sidecar",
    )
    assert path == f"{TGT_PREFIX}p.jpg.xmp"


async def test_sidecar_path_is_not_returned_for_rollback_cleanup(conn):
    """The returned paths get unlinked on rollback. The XMP is not ours to delete."""
    from src.asset_sync import _sync_asset_files

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    src_asset = await make_asset(conn, src, original_path=f"{SRC_PREFIX}p.jpg")
    tgt_asset = await make_asset(conn, tgt, original_path=f"{TGT_PREFIX}p.jpg")
    await _add_file(conn, src_asset, "sidecar", f"{SRC_PREFIX}p.jpg.xmp")

    created = await _sync_asset_files(conn, src_asset, tgt_asset, src, tgt, _job(src, tgt))

    assert all(".xmp" not in p for p in created), created


async def test_sidecar_outside_the_job_prefix_is_skipped(conn):
    """A path the job prefixes cannot remap must not be guessed at."""
    from src.asset_sync import _sync_asset_files

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    src_asset = await make_asset(conn, src, original_path=f"{SRC_PREFIX}p.jpg")
    tgt_asset = await make_asset(conn, tgt, original_path=f"{TGT_PREFIX}p.jpg")
    await _add_file(conn, src_asset, "sidecar", "/somewhere/else/p.jpg.xmp")

    await _sync_asset_files(conn, src_asset, tgt_asset, src, tgt, _job(src, tgt))

    rows = await conn.fetchval(
        'SELECT count(*) FROM asset_file WHERE "assetId" = $1 AND type = $2',
        tgt_asset, "sidecar",
    )
    assert rows == 0


async def test_cleanup_never_unlinks_a_sidecar_path(conn, monkeypatch):
    """cleanup_deleted_assets must not hand the XMP to remove_hardlinks.

    The target XMP path resolves through the external-library symlink to the
    source user's own file, so unlinking it destroys source data.
    """
    import src.cleanup as cleanup_mod

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)

    await _add_file(conn, tgt_asset, "thumbnail", "/data/thumbs/t/aa/bb/x_thumbnail.webp")
    await _add_file(conn, tgt_asset, "sidecar", f"{TGT_PREFIX}p.jpg.xmp")

    # Map the pair, then hard-delete the source so cleanup targets it.
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        src_asset, tgt_asset, src, tgt,
    )
    await conn.execute("DELETE FROM asset WHERE id = $1", src_asset)

    handed_over: list[str] = []
    monkeypatch.setattr(cleanup_mod, "remove_hardlinks", handed_over.extend)

    await cleanup_mod.cleanup_deleted_assets(conn)

    assert handed_over, "cleanup did not run"
    assert all(".xmp" not in p for p in handed_over), handed_over


async def test_delete_synced_never_unlinks_a_sidecar_path(conn, monkeypatch):
    """The same guarantee as above, for the interactive bulk-delete tool.

    delete_synced.py is the higher-consequence path -- it removes every synced
    asset for a user in one go -- and it kept handing XMP paths to
    remove_hardlinks long after cleanup.py stopped. remove_hardlinks rejects
    them today via validate_path_within_upload, so this test pins the
    behaviour at the SQL, where the reason lives, rather than at the backstop.
    """
    import delete_synced as delete_synced_mod

    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    tgt_asset = await make_asset(conn, tgt)

    await _add_file(conn, tgt_asset, "thumbnail", "/data/thumbs/t/aa/bb/x_thumbnail.webp")
    await _add_file(conn, tgt_asset, "sidecar", f"{TGT_PREFIX}p.jpg.xmp")
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        await make_asset(conn, src), tgt_asset, src, tgt,
    )

    handed_over: list[str] = []
    monkeypatch.setattr(delete_synced_mod, "remove_hardlinks", handed_over.extend)

    assert await delete_synced_mod.delete_synced_asset(conn, tgt_asset) is True

    assert handed_over, "delete_synced_asset handed over no paths at all"
    assert all(".xmp" not in p for p in handed_over), handed_over
