from uuid import uuid4

import src.person_sync as person_sync
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group,
    make_synced_pair, make_user,
)
from src.person_sync import (
    cleanup_orphaned_persons,
    delete_target_person_in_shared_group,
    ensure_target_person,
    sync_person_names,
    sync_person_thumbnails,
)


async def _map_synced_pair(conn, src, tgt, *, person_group_id=None):
    """Give (src, tgt) a _face_sync_asset_map row, as Phase 1 would after
    syncing at least one asset — this is what puts a pair in scope for the
    cross-user sync/cleanup functions below.

    Pass ``person_group_id`` to also put a face in that group on the synced
    target asset, which is what brings the group itself into scope for the
    metadata syncs. Returns the target asset id.

    Thin wrapper over ``make_synced_pair`` kept for the shorter return value,
    which most tests in this file want.
    """
    _, tgt_asset = await make_synced_pair(
        conn, src, tgt, person_group_id=person_group_id,
    )
    return tgt_asset


async def _decoy_map_row(conn):
    """A mapped pair of users unrelated to the test's own users.

    Without this, a test asserting "the stranger's row survived" proves
    nothing: inside the rolled-back transaction _face_sync_asset_map is empty,
    so *every* EXISTS over it is false and the statement under test cannot do
    anything at all. The decoy makes the table non-empty so the scoping
    predicate is actually exercised."""
    cg = await make_cluster_group(conn)
    other_src = await make_user(conn, cluster_group_id=cg)
    other_tgt = await make_user(conn, cluster_group_id=cg)
    await _map_synced_pair(conn, other_src, other_tgt)
    return other_src, other_tgt


async def test_creates_target_person_row_for_shared_group(conn, pair):
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")

    result = await ensure_target_person(conn, pg, src, tgt)

    assert result == pg
    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Granny"


async def test_is_idempotent(conn, pair):
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")

    await ensure_target_person(conn, pg, src, tgt)
    await ensure_target_person(conn, pg, src, tgt)

    count = await conn.fetchval(
        'SELECT COUNT(*) FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert count == 1


async def test_sync_person_names_fills_empty_name(conn, pair):
    """The positive path: an empty target name gets filled from the source."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")
    await make_person(conn, tgt, pg, name="")
    await _map_synced_pair(conn, src, tgt, person_group_id=pg)

    updated = await sync_person_names(conn)

    assert updated == 1
    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Granny"


async def test_does_not_overwrite_a_name_the_target_already_set(conn, pair):
    """The target user's own naming wins — we only fill an empty name."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")
    await make_person(conn, tgt, pg, name="Nana")
    await _map_synced_pair(conn, src, tgt)

    updated = await sync_person_names(conn)

    assert updated == 0
    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Nana"


async def test_is_hidden_is_inherited_when_the_person_row_is_created(conn, pair):
    """A hidden source person starts hidden for the target too."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await conn.execute(
        'INSERT INTO person ("ownerId", "personGroupId", name, "isHidden") VALUES ($1, $2, $3, TRUE)',
        src, pg, "Granny",
    )

    await ensure_target_person(conn, pg, src, tgt)

    is_hidden = await conn.fetchval(
        'SELECT "isHidden" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert is_hidden is True


async def test_no_cycle_function_overwrites_the_target_visibility(conn, pair):
    """Visibility is per-user. Nothing a sync cycle runs may overwrite it.

    isHidden is a plain boolean with no "unset" sentinel, so there is no
    fill-only middle ground the way there is for name. Either the sidecar owns
    it or the user does, and the user does.

    This calls every public ``async def f(conn)`` in person_sync rather than a
    fixed list, so a newly added sync function is covered automatically — which
    is the actual regression risk, since removing this behaviour was a
    deliberate decision someone could just as deliberately undo.
    """
    import inspect

    import src.person_sync as person_sync

    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await conn.execute(
        'INSERT INTO person ("ownerId", "personGroupId", name, "isHidden") VALUES ($1, $2, $3, TRUE)',
        src, pg, "Granny",
    )
    await conn.execute(
        'INSERT INTO person ("ownerId", "personGroupId", name, "isHidden") VALUES ($1, $2, $3, FALSE)',
        tgt, pg, "",
    )
    await _map_synced_pair(conn, src, tgt, person_group_id=pg)

    # Every public coroutine in person_sync that a sync cycle can call with
    # just a connection. The filter is "first parameter is conn, the rest have
    # defaults" rather than an exact ["conn"] match: Phase 1 is
    # job-parameterised, so a future sync_person_visibility(conn, job) is the
    # plausible shape, and an exact match would skip it in silence -- this
    # guard would stop guarding and say nothing.
    discovered = []
    for name, fn in inspect.getmembers(person_sync, inspect.iscoroutinefunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters.values())
        if not params or params[0].name != "conn":
            continue
        if any(p.default is inspect.Parameter.empty for p in params[1:]):
            # Needs arguments this test cannot supply. Not skipped quietly:
            # the assertion below fails until someone decides what to do.
            discovered.append((name, False))
            continue
        discovered.append((name, True))

    callable_here = [n for n, ok in discovered if ok]
    for name in callable_here:
        await getattr(person_sync, name)(conn)

    # Pinned explicitly, so adding a function to person_sync forces a decision
    # here instead of silently widening or narrowing what this guard covers.
    assert sorted(n for n, _ in discovered) == [
        "cleanup_orphaned_persons",
        "delete_target_person_in_shared_group",
        "ensure_target_person",
        "sync_person_names",
        "sync_person_thumbnails",
    ], discovered
    assert sorted(callable_here) == [
        "cleanup_orphaned_persons", "sync_person_names", "sync_person_thumbnails",
    ], discovered
    is_hidden = await conn.fetchval(
        'SELECT "isHidden" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert is_hidden is False, f"visibility overwritten by one of: {callable_here}"

async def test_returns_none_when_source_has_no_person_row(conn, pair):
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)  # no person row for src

    assert await ensure_target_person(conn, pg, src, tgt) is None


async def test_cleanup_removes_target_person_with_no_faces(conn, pair):
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)  # target person; source has none; no faces
    await _map_synced_pair(conn, src, tgt)

    removed = await cleanup_orphaned_persons(conn)

    assert removed == 1
    exists = await conn.fetchval(
        'SELECT 1 FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert exists is None


async def test_cleanup_keeps_target_person_that_still_has_faces(conn, pair):
    """Deleting a person whose faces remain would null those faces."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)
    asset = await make_asset(conn, tgt)
    await make_face(conn, asset, person_group_id=pg)
    await _map_synced_pair(conn, src, tgt)

    removed = await cleanup_orphaned_persons(conn)

    assert removed == 0


async def test_cleanup_ignores_persons_of_unmanaged_users(conn):
    """A user the sidecar never syncs must not have persons deleted.

    The decoy map row is load-bearing: it makes _face_sync_asset_map non-empty
    so the scoping predicate is really evaluated. Without it this test passes
    even if the DELETE were scoped on the wrong column — or not scoped at all.
    """
    await _decoy_map_row(conn)

    cg = await make_cluster_group(conn)
    stranger = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, stranger, pg, name="Not ours")  # no faces, no map row

    assert await cleanup_orphaned_persons(conn) == 0
    survived = await conn.fetchval(
        'SELECT count(*) FROM person WHERE "ownerId" = $1', stranger
    )
    assert survived == 1


async def test_cleanup_keeps_person_while_the_source_still_has_faces_in_the_group(conn, pair):
    """The face guard is group-scoped, not owner-scoped.

    Deleting the last person row in a group makes Immich's deleteEmptyGroups
    null *every* face in it, including the source user's faces on the source
    user's own photos. This is the only statement in the sidecar that can
    write into the source's library, so it must refuse while any face
    anywhere still points at the group.
    """
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)

    # Source's own photo, with a face in the shared group. The source's person
    # row for the group is gone (they deleted the person in Immich).
    src_asset = await make_asset(conn, src)
    src_face = await make_face(conn, src_asset, person_group_id=pg)
    await make_person(conn, tgt, pg)  # target row survives; target has no faces
    await _map_synced_pair(conn, src, tgt)

    assert await cleanup_orphaned_persons(conn) == 0
    survived = await conn.fetchval(
        'SELECT 1 FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert survived == 1
    still_assigned = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE id = $1', src_face
    )
    assert still_assigned == pg


async def test_cleanup_keeps_person_while_a_trashed_source_face_holds_the_group(conn, pair):
    """Same as above, but the source's remaining face is soft-deleted.

    A trashed asset_face still carries its "personGroupId" and the FK is
    ON DELETE SET NULL, so emptying the group nulls it exactly like a live
    face — invisibly, and unrecoverably: restoring the asset from trash does
    not bring the assignment back. A `deletedAt IS NULL` filter on the face
    guard made this state deletion-eligible while every other guard passed.
    """
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    src_face = await make_face(conn, src_asset, person_group_id=pg)
    await conn.execute(
        'UPDATE asset_face SET "deletedAt" = NOW() WHERE id = $1', src_face
    )
    await make_person(conn, tgt, pg)
    await _map_synced_pair(conn, src, tgt)

    assert await cleanup_orphaned_persons(conn) == 0
    survived = await conn.fetchval(
        'SELECT 1 FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert survived == 1
    still_assigned = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE id = $1', src_face
    )
    assert still_assigned == pg


async def test_cleanup_keeps_group_known_to_only_one_of_two_sources(conn):
    """Two jobs into one target: the "source has no person row" guard must hold
    for *every* mapped source, not merely for one of them. Nested inside the
    map EXISTS, the negation is satisfied by whichever source happens not to
    know the group — which, since a group is typically known to one source
    only, makes every group deletion-eligible."""
    cg = await make_cluster_group(conn)
    src_a = await make_user(conn, cluster_group_id=cg)
    src_b = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    await _map_synced_pair(conn, src_a, tgt)
    await _map_synced_pair(conn, src_b, tgt)
    await make_person(conn, src_b, pg, name="Granny")  # src_a knows nothing of pg
    await make_person(conn, tgt, pg, name="Granny")

    assert await cleanup_orphaned_persons(conn) == 0
    survived = await conn.fetchval(
        'SELECT 1 FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert survived == 1


async def test_sync_person_names_ignores_unmapped_user_pairs(conn):
    """Two users in the same cluster group but with no sync relationship must
    not have names copied between them."""
    await _decoy_map_row(conn)

    cg = await make_cluster_group(conn)
    stranger_a = await make_user(conn, cluster_group_id=cg)
    stranger_b = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, stranger_a, pg, name="Granny")
    await make_person(conn, stranger_b, pg, name="")

    assert await sync_person_names(conn) == 0
    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        stranger_b, pg,
    )
    assert name == ""




async def test_sync_person_names_skips_groups_no_synced_asset_carries(conn, pair):
    """Names must only reach person groups the sidecar actually synced.

    Under cluster groups Immich puts both users' faces into the same
    person_group by construction, so "same group + mapped owner pair" is not a
    sidecar footprint. Without the synced-asset guard, the source's name lands
    on a person the target discovered entirely on their own photos.
    """
    cg, src, tgt = pair

    carried = await make_person_group(conn, cg)      # reached via a synced asset
    own_only = await make_person_group(conn, cg)     # target's own discovery

    for pg in (carried, own_only):
        await make_person(conn, src, pg, name="Granny")
        await make_person(conn, tgt, pg, name="")

    # Only `carried` is present on the synced target asset.
    await _map_synced_pair(conn, src, tgt, person_group_id=carried)

    # The target's own photo carries `own_only`. It is not a synced asset.
    own_asset = await make_asset(conn, tgt)
    await make_face(conn, own_asset, person_group_id=own_only, bbox=(2, 2, 8, 8))

    assert await sync_person_names(conn) == 1

    assert await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, carried
    ) == "Granny"
    assert await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, own_only
    ) == ""


async def test_teardown_deletes_the_target_row_when_a_source_still_holds_the_group(conn, pair):

    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg)
    await make_person(conn, tgt, pg)
    await _map_synced_pair(conn, src, tgt)

    assert await delete_target_person_in_shared_group(conn, tgt, pg) is True
    assert await conn.fetchval(
        'SELECT count(*) FROM person WHERE "ownerId" = $1', tgt
    ) == 0
    # The source keeps its own row, so the group never empties.
    assert await conn.fetchval(
        'SELECT count(*) FROM person WHERE "ownerId" = $1', src
    ) == 1


async def test_teardown_refuses_when_no_mapped_source_holds_the_group(conn, pair):
    """This is the guard that stops the group emptying, not the face guard.

    Deleting the last person row on a group lets Immich's deleteEmptyGroups
    drop it and null every face in it -- the source user's included. Requiring
    a mapped source to still hold a row is what makes that impossible. It is
    re-checked here at delete time because delete_synced.py lists its
    candidates before an interactive prompt, so the source row can disappear
    between the listing and the delete.
    """

    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)  # source row is gone
    await _map_synced_pair(conn, src, tgt)

    assert await delete_target_person_in_shared_group(conn, tgt, pg) is False
    assert await conn.fetchval(
        'SELECT count(*) FROM person WHERE "ownerId" = $1', tgt
    ) == 1


async def test_teardown_refuses_while_the_target_still_has_faces_in_the_group(conn, pair):

    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg)
    await make_person(conn, tgt, pg)
    await _map_synced_pair(conn, src, tgt)

    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, tgt_asset, person_group_id=pg)

    assert await delete_target_person_in_shared_group(conn, tgt, pg) is False
    assert await conn.fetchval(
        'SELECT count(*) FROM person WHERE "ownerId" = $1', tgt
    ) == 1


async def test_teardown_face_guard_is_owner_scoped_not_group_scoped(conn, pair):
    """Deliberately unlike cleanup_orphaned_persons, which is group-scoped.

    Teardown runs while the source still owns faces in the group -- that is
    the normal state -- so a group-scoped face guard would refuse every
    deletion and make the teardown scripts silent no-ops. It is safe here
    precisely because the EXISTS above keeps a source person row on the group,
    so nothing can empty it.
    """

    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg)
    await make_person(conn, tgt, pg)
    await _map_synced_pair(conn, src, tgt)

    src_asset = await make_asset(conn, src)
    await make_face(conn, src_asset, person_group_id=pg)

    assert await delete_target_person_in_shared_group(conn, tgt, pg) is True
    # The source's face survives, still assigned, because the group kept a row.
    assert await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', src_asset
    ) == pg


async def test_orphan_cleanup_spares_a_named_person(conn, pair):
    """A name is the only provenance left, so it is treated as one.

    _face_sync_person_map used to record which target rows the sidecar
    created; v3.2.0 retired it, and the remaining guards — managed target, no
    source person on the group, no faces anywhere — are all satisfied by a
    person the target user found on their own photos and then deleted the
    photos of. Deleting that is something Immich itself would never do, so an
    unnamed row is fair game and a named one is not.
    """
    cg, src, tgt = pair
    await make_synced_pair(conn, src, tgt)

    named_group = await make_person_group(conn, cg)
    await make_person(conn, tgt, named_group, name="Someone the target named")

    unnamed_group = await make_person_group(conn, cg)
    await make_person(conn, tgt, unnamed_group, name="")

    assert await cleanup_orphaned_persons(conn) == 1

    survivors = {
        r["personGroupId"] for r in await conn.fetch(
            'SELECT "personGroupId" FROM person WHERE "ownerId" = $1', tgt
        )
    }
    assert named_group in survivors
    assert unnamed_group not in survivors


# --- Person thumbnails ---------------------------------------------------
#
# Since v3.2.0 the path is keyed on the person *group*, not the person:
#   {upload}/thumbs/{ownerId}/{pgid[0:2]}/{pgid[2:4]}/{pgid}{suffix}
# Source and target share the group id, so only the owner directory changes.
# Getting a shard slice or the owner wrong produces a plausible-looking path
# that nothing else notices: _hardlink_person_thumbnail returns "" on any
# OSError and sync_person_thumbnails silently counts zero.


def _thumb_for(base, owner, pgid, suffix=".jpeg"):
    pg = str(pgid)
    return base / "thumbs" / str(owner) / pg[:2] / pg[2:4] / f"{pg}{suffix}"


async def _seed_source_thumbnail(conn, tmp_path, monkeypatch, src, pg):
    """Put a real file where the source person's thumbnail lives."""
    monkeypatch.setattr(person_sync.settings, "upload_location_mount", str(tmp_path))
    source_file = _thumb_for(tmp_path, src, pg)
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(b"jpeg-bytes")
    await conn.execute(
        'UPDATE person SET "thumbnailPath" = $1 WHERE "ownerId" = $2 AND "personGroupId" = $3',
        str(source_file), src, pg,
    )
    return source_file


async def test_thumbnail_is_hardlinked_to_the_group_keyed_target_path(
    conn, pair, tmp_path, monkeypatch
):
    """The v3.2.0 path shape, asserted exactly rather than by "a file appeared"."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")
    await make_person(conn, tgt, pg, name="Granny")
    await _map_synced_pair(conn, src, tgt, person_group_id=pg)
    source_file = await _seed_source_thumbnail(conn, tmp_path, monkeypatch, src, pg)

    assert await sync_person_thumbnails(conn) == 1

    expected = _thumb_for(tmp_path, tgt, pg)
    assert expected.exists(), f"nothing at {expected}"
    assert expected.stat().st_ino == source_file.stat().st_ino, "copied, not hardlinked"
    stored = await conn.fetchval(
        'SELECT "thumbnailPath" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        tgt, pg,
    )
    assert stored == str(expected)


async def test_an_existing_target_thumbnail_is_left_alone(
    conn, pair, tmp_path, monkeypatch
):
    """Fill-only: the target owns their thumbnail once they have one."""
    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")
    await make_person(conn, tgt, pg, name="Granny")
    await _map_synced_pair(conn, src, tgt, person_group_id=pg)
    await _seed_source_thumbnail(conn, tmp_path, monkeypatch, src, pg)
    await conn.execute(
        'UPDATE person SET "thumbnailPath" = $1 WHERE "ownerId" = $2 AND "personGroupId" = $3',
        "/their/own/choice.jpeg", tgt, pg,
    )

    assert await sync_person_thumbnails(conn) == 0

    stored = await conn.fetchval(
        'SELECT "thumbnailPath" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        tgt, pg,
    )
    assert stored == "/their/own/choice.jpeg"


async def test_thumbnails_are_not_linked_for_a_group_no_synced_asset_carries(
    conn, pair, tmp_path, monkeypatch
):
    """_SYNCED_GROUP_SCOPE, on the one cross-user write that creates files.

    The decoy map row is load-bearing here for the same reason as elsewhere:
    without it every EXISTS over _face_sync_asset_map is false and the
    statement cannot do anything at all, so the assertion proves nothing.
    """
    await _decoy_map_row(conn)

    cg, src, tgt = pair
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Stranger")
    await make_person(conn, tgt, pg, name="Stranger")
    # Mapped pair, but no synced asset carries a face in this group: the target
    # found this person on their own photos.
    await _map_synced_pair(conn, src, tgt)
    await _seed_source_thumbnail(conn, tmp_path, monkeypatch, src, pg)

    assert await sync_person_thumbnails(conn) == 0
    assert not _thumb_for(tmp_path, tgt, pg).exists()


def test_a_thumbnail_outside_the_upload_directory_is_refused(tmp_path, monkeypatch):
    """The path comes from Immich's database, so it is not ours to trust."""
    monkeypatch.setattr(person_sync.settings, "upload_location_mount", str(tmp_path))

    result = person_sync._hardlink_person_thumbnail(
        person_group_id=uuid4(),
        target_user_id=uuid4(),
        source_thumbnail_path="/etc/passwd",
    )

    assert result == ""


def test_a_missing_source_file_is_not_recorded_as_linked(tmp_path, monkeypatch):
    """Returning a path for a file that was never linked would leave the target
    person pointing at nothing, and sync_person_thumbnails counting it."""
    monkeypatch.setattr(person_sync.settings, "upload_location_mount", str(tmp_path))
    pg, tgt = uuid4(), uuid4()

    result = person_sync._hardlink_person_thumbnail(
        person_group_id=pg,
        target_user_id=tgt,
        source_thumbnail_path=str(_thumb_for(tmp_path, uuid4(), pg)),
    )

    assert result == ""
