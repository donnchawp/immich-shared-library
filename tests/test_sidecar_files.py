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

import src.cleanup as cleanup_mod
from src.asset_sync import _sync_asset_files
from src.config import SyncJob
from tests.conftest import make_asset, make_synced_pair

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


async def test_sidecar_row_is_created_with_a_remapped_path(conn, pair):

    cg, src, tgt = pair
    src_asset = await make_asset(conn, src, original_path=f"{SRC_PREFIX}p.jpg")
    tgt_asset = await make_asset(conn, tgt, original_path=f"{TGT_PREFIX}p.jpg")
    await _add_file(conn, src_asset, "sidecar", f"{SRC_PREFIX}p.jpg.xmp")

    await _sync_asset_files(conn, src_asset, tgt_asset, src, tgt, _job(src, tgt))

    path = await conn.fetchval(
        'SELECT path FROM asset_file WHERE "assetId" = $1 AND type = $2',
        tgt_asset, "sidecar",
    )
    assert path == f"{TGT_PREFIX}p.jpg.xmp"


async def test_sidecar_path_is_not_returned_for_rollback_cleanup(conn, pair):
    """The returned paths get unlinked on rollback. The XMP is not ours to delete."""

    cg, src, tgt = pair
    src_asset = await make_asset(conn, src, original_path=f"{SRC_PREFIX}p.jpg")
    tgt_asset = await make_asset(conn, tgt, original_path=f"{TGT_PREFIX}p.jpg")
    await _add_file(conn, src_asset, "sidecar", f"{SRC_PREFIX}p.jpg.xmp")

    created = await _sync_asset_files(conn, src_asset, tgt_asset, src, tgt, _job(src, tgt))

    assert all(".xmp" not in p for p in created), created


async def test_sidecar_outside_the_job_prefix_is_skipped(conn, pair):
    """A path the job prefixes cannot remap must not be guessed at."""

    cg, src, tgt = pair
    src_asset = await make_asset(conn, src, original_path=f"{SRC_PREFIX}p.jpg")
    tgt_asset = await make_asset(conn, tgt, original_path=f"{TGT_PREFIX}p.jpg")
    await _add_file(conn, src_asset, "sidecar", "/somewhere/else/p.jpg.xmp")

    await _sync_asset_files(conn, src_asset, tgt_asset, src, tgt, _job(src, tgt))

    rows = await conn.fetchval(
        'SELECT count(*) FROM asset_file WHERE "assetId" = $1 AND type = $2',
        tgt_asset, "sidecar",
    )
    assert rows == 0


async def test_cleanup_never_unlinks_a_sidecar_path(conn, pair, monkeypatch):
    """cleanup_deleted_assets must not hand the XMP to remove_hardlinks.

    The target XMP path resolves through the external-library symlink to the
    source user's own file, so unlinking it destroys source data.
    """
    cg, src, tgt = pair
    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)

    await _add_file(conn, tgt_asset, "thumbnail", "/data/thumbs/t/aa/bb/x_thumbnail.webp")
    await _add_file(conn, tgt_asset, "sidecar", f"{TGT_PREFIX}p.jpg.xmp")

    # Map the pair, then hard-delete the source so cleanup targets it.
    await make_synced_pair(
        conn, src, tgt,
        source_asset_id=src_asset, target_asset_id=tgt_asset,
    )
    await conn.execute("DELETE FROM asset WHERE id = $1", src_asset)

    handed_over: list[str] = []
    monkeypatch.setattr(cleanup_mod, "remove_hardlinks", handed_over.extend)

    await cleanup_mod.cleanup_deleted_assets(conn)

    assert handed_over, "cleanup did not run"
    assert all(".xmp" not in p for p in handed_over), handed_over


async def test_delete_synced_deletes_assets_through_the_shared_helper():
    """delete_synced.py must not grow its own copy of the asset deletion.

    It is the higher-consequence path -- it removes every synced asset for a
    user in one go -- and it once kept handing XMP paths to remove_hardlinks
    long after cleanup.py stopped, because it carried a hand-maintained
    duplicate of cleanup_deleted_assets' body. The sidecar guarantee above is
    tested once, at the helper; this pins the only way it can drift again.
    """
    import delete_synced as delete_synced_mod

    assert delete_synced_mod.delete_target_asset is cleanup_mod.delete_target_asset
