from uuid import uuid4

from src.ml_sync import sync_faces_for_asset, sync_faces_incremental
from tests.conftest import (
    make_asset, make_face, make_person, make_person_group,
    make_synced_pair,
)


async def test_copies_person_group_id_verbatim(conn, pair):
    cg, src, tgt = pair
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


async def test_creates_the_target_person_row(conn, pair):
    cg, src, tgt = pair
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


async def test_unassigned_face_copies_with_null_group(conn, pair):
    cg, src, tgt = pair

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=None)

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 1
    copied = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied is None


async def test_does_not_duplicate_an_existing_bounding_box(conn, pair):
    cg, src, tgt = pair

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


async def test_sets_face_asset_id_on_the_target_person(conn, pair):
    cg, src, tgt = pair
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


async def test_incremental_sync_skips_mappings_whose_target_asset_is_gone(conn, pair):
    """A hard-deleted target asset must not crash the whole cycle.

    Phase 2 reads pairs straight out of _face_sync_asset_map. When the target
    user hard-deletes a synced asset the mapping row survives until Phase 4's
    cleanup_stale_mappings prunes it — but Phase 4 runs *after* Phase 2, so an
    unguarded Phase 2 raises ForeignKeyViolationError, aborts the run, and the
    cleanup that would have fixed it never executes. That deadlocks the sidecar
    on every subsequent cycle.
    """

    cg, src, tgt = pair

    src_asset = await make_asset(conn, src)
    missing_target = uuid4()  # never inserted into asset

    # synced_at in the past so the source face counts as updated since
    await make_synced_pair(
        conn, src, tgt,
        source_asset_id=src_asset, target_asset_id=missing_target,
        synced_at="NOW() - INTERVAL '1 day'",
    )
    await make_face(conn, src_asset, bbox=(1, 1, 9, 9))

    # Must not raise; the stale pair is simply skipped for Phase 4 to prune.
    assert await sync_faces_incremental(conn) == 0


async def test_incremental_sync_still_syncs_live_pairs(conn, pair):
    """The guard must not stop real work — pairs with a live target still sync."""

    cg, src, tgt = pair

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_synced_pair(
        conn, src, tgt,
        source_asset_id=src_asset, target_asset_id=tgt_asset,
        synced_at="NOW() - INTERVAL '1 day'",
    )
    await make_face(conn, src_asset, bbox=(2, 2, 8, 8))

    assert await sync_faces_incremental(conn) == 1
    copied = await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied == 1


async def test_one_failing_pair_does_not_poison_the_incremental_phase(conn, pair):
    """Phase 2's loop needs the same per-item savepoint as Phase 1's.

    A failed statement leaves Postgres in an aborted transaction, so one bad
    pair used to take down the whole pass — and because a failed pair keeps
    its old watermark, it stays in the window and kills the pass again on
    every later cycle. The failure must be contained and the good pair must
    still sync.
    """

    cg, src, tgt = pair

    doomed_src = await make_asset(conn, src)
    doomed_tgt = await make_asset(conn, tgt)
    good_src = await make_asset(conn, src)
    good_tgt = await make_asset(conn, tgt)
    for s, t in ((doomed_src, doomed_tgt), (good_src, good_tgt)):
        await make_synced_pair(
            conn, src, tgt,
            source_asset_id=s, target_asset_id=t,
            synced_at="NOW() - INTERVAL '1 day'",
        )
    await make_face(conn, doomed_src, bbox=(1, 1, 9, 9))
    await make_face(conn, good_src, bbox=(2, 2, 8, 8))

    # imageWidth copies verbatim, so tagging the doomed source face makes the
    # trigger fire for its copy and no other.
    await conn.execute(
        'UPDATE asset_face SET "imageWidth" = 666 WHERE "assetId" = $1', doomed_src
    )
    await conn.execute(
        """
        CREATE FUNCTION _refuse_face() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'no faces for you'; END $$;
        CREATE TRIGGER _refuse_face_ins BEFORE INSERT ON asset_face
            FOR EACH ROW WHEN (NEW."imageWidth" = 666)
            EXECUTE FUNCTION _refuse_face();
        """
    )

    assert await sync_faces_incremental(conn) == 1
    assert await conn.fetchval("SELECT 1") == 1

    assert await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "assetId" = $1', good_tgt
    ) == 1
    assert await conn.fetchval(
        'SELECT count(*) FROM asset_face WHERE "assetId" = $1', doomed_tgt
    ) == 0

    # The good pair's watermark moves; the doomed pair's stays in the past so
    # the next cycle tries it again.
    watermarks = dict(
        (r["source_asset_id"], r["synced_at"])
        for r in await conn.fetch(
            "SELECT source_asset_id, synced_at FROM _face_sync_asset_map"
        )
    )
    assert watermarks[good_src] > watermarks[doomed_src]


async def test_copied_face_gets_no_embedding(conn, pair):
    """A copy must not be a facial-recognition candidate.

    searchFaces() inner-joins face_search, and a copied embedding would be
    byte-identical to its source — a distance-0 twin that votes a second time
    when recognition counts matches against minFaces.
    """
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    src_face = await make_face(conn, src_asset, person_group_id=pg)
    await conn.execute(
        'INSERT INTO face_search ("faceId", embedding) VALUES ($1, $2)',
        src_face, "[" + ",".join(["0.1"] * 512) + "]",
    )

    await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    # The source keeps its embedding; the copy never gets one.
    assert await conn.fetchval(
        'SELECT count(*) FROM face_search WHERE "faceId" = $1', src_face
    ) == 1
    copied_embeddings = await conn.fetchval(
        """
        SELECT count(*) FROM face_search fs
        JOIN asset_face af ON af.id = fs."faceId"
        WHERE af."assetId" = $1
        """,
        tgt_asset,
    )
    assert copied_embeddings == 0


async def test_copied_face_is_not_machine_learning(conn, pair):
    """Recognition skips a non-machine-learning face before it looks for an
    embedding, so this is what keeps the copies out of the queue rather than
    failing in it. 'manual' and not 'exif': metadata extraction deletes every
    exif-sourced face on an asset and rebuilds it from XMP regions.
    """
    cg, src, tgt = pair

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=None)
    assert await conn.fetchval(
        'SELECT "sourceType"::text FROM asset_face WHERE "assetId" = $1', src_asset
    ) == "machine-learning"

    await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert await conn.fetchval(
        'SELECT "sourceType"::text FROM asset_face WHERE "assetId" = $1', tgt_asset
    ) == "manual"


async def test_a_reassignment_only_pass_still_advances_the_watermark(conn, pair):
    """A pair that copies nothing must still leave the Phase 2 window.

    Reassigning a source face to a different person group bumps
    asset_face."updatedAt", so Phase 2 selects the pair — but the bounding box
    is already on the target, so nothing is inserted and the count is 0.
    Gating the watermark update on count > 0 left the pair inside the window
    permanently, re-fetched and re-scanned on every cycle for the life of the
    install. The reassignment itself is Phase 4's job, not this one's.
    """
    cg, src, tgt = pair
    first = await make_person_group(conn, cg)
    second = await make_person_group(conn, cg)
    await make_person(conn, src, first)
    await make_person(conn, src, second)

    src_asset, tgt_asset = await make_synced_pair(
        conn, src, tgt, synced_at="NOW() - INTERVAL '1 day'",
    )
    await make_face(conn, src_asset, person_group_id=first, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=first, bbox=(1, 1, 9, 9))

    # The source user moves the face to a different person. Only "updatedAt"
    # changes as far as Phase 2 is concerned; the box is untouched.
    await conn.execute(
        'UPDATE asset_face SET "personGroupId" = $1 WHERE "assetId" = $2',
        second, src_asset,
    )

    before = await conn.fetchval(
        "SELECT synced_at FROM _face_sync_asset_map WHERE source_asset_id = $1", src_asset
    )
    assert await sync_faces_incremental(conn) == 0

    after = await conn.fetchval(
        "SELECT synced_at FROM _face_sync_asset_map WHERE source_asset_id = $1", src_asset
    )
    assert after > before, "the pair would be re-scanned every cycle forever"

    # And the window is genuinely closed: a second pass finds nothing to do.
    assert await sync_faces_incremental(conn) == 0
