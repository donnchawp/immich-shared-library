#!/usr/bin/env python3
"""
Delete all synced assets for a target user.

Removes every asset the sync engine created for a given target user,
along with mirrored persons and their thumbnail hardlinks. Does NOT
mark sources as skipped — running the sync engine again will recreate
the assets, which is the expected behavior for a "reset" operation.

Usage:
  python3 delete_synced.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

# Load .env before importing anything from src: src.config builds its settings
# singleton at import time, so the environment has to be populated first.
#
# A missing .env is not fatal here, only in main(). Importing this module must
# stay side-effect-free enough for the test suite to reach the functions below
# — .env is gitignored, so an import-time sys.exit would make every test of
# this file pass only on a machine that happens to have one.
ENV_FILE = Path(__file__).parent / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and value:
            os.environ.setdefault(key.strip(), value.strip())

os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.db import init_pool, close_pool, fetch_all, fetch_one, transaction
from src.file_ops import owned_file_paths, remove_hardlinks
from src.main import ensure_tracking_tables
from src.person_sync import delete_target_person_in_shared_group


async def get_target_users() -> list[dict]:
    """Get distinct target users from the asset mapping table, with email for display."""
    rows = await fetch_all("""
        SELECT DISTINCT m.target_user_id, u.email, u.name
        FROM _face_sync_asset_map m
        JOIN "user" u ON u.id = m.target_user_id
        ORDER BY u.email
    """)
    return [{"id": r["target_user_id"], "email": r["email"], "name": r["name"]} for r in rows]


async def get_synced_assets(target_user_id) -> list[dict]:
    """Get all synced assets for a target user."""
    rows = await fetch_all("""
        SELECT m.source_asset_id, m.target_asset_id
        FROM _face_sync_asset_map m
        WHERE m.target_user_id = $1
    """, target_user_id)
    return [dict(r) for r in rows]


async def get_mirrored_persons(target_user_id) -> list[dict]:
    """Get sidecar-created target persons for a target user.

    v3.2.0 cluster groups: person identity is Immich's shared person_group,
    so there is no more person-mapping table. A "mirrored person" is
    identified structurally: a person row owned by this target user, on a
    personGroupId that a paired source user (per _face_sync_asset_map) also
    has a person row for.

    Note this is the inverse of cleanup_orphaned_persons' selection, which
    wants groups no mapped source holds any more. This listing is advisory
    only — the same condition is re-checked inside the delete, because the
    user is prompted in between.
    """
    rows = await fetch_all("""
        SELECT t."personGroupId" AS person_group_id
        FROM person t
        WHERE t."ownerId" = $1
          AND EXISTS (
              SELECT 1 FROM _face_sync_asset_map m
              JOIN person s ON s."personGroupId" = t."personGroupId"
                            AND s."ownerId" = m.source_user_id
              WHERE m.target_user_id = $1
          )
    """, target_user_id)
    return [dict(r) for r in rows]


async def delete_synced_asset(conn, target_asset_id) -> bool:
    """Delete a synced target asset.

    Follows the same pattern as cleanup_deleted_assets:
    remove hardlinks -> delete album_asset -> delete asset (cascades) -> delete mapping.
    Does NOT insert into _face_sync_skipped.

    Caller batches these inside one transaction(), so the failure path needs a
    SAVEPOINT: without one, Postgres aborts the whole transaction on the first
    error and every later statement raises InFailedSQLTransactionError. Same
    pattern as sync_asset (src/asset_sync.py).
    """
    await conn.execute("SAVEPOINT delete_synced_asset")
    try:
        # Paths we created and may delete — excludes the XMP sidecar, which
        # is not ours. See file_ops.owned_file_paths.
        file_paths = await owned_file_paths(conn, target_asset_id)

        # Remove hardlinked files first
        remove_hardlinks(file_paths)

        # Remove from albums
        await conn.execute(
            'DELETE FROM album_asset WHERE "assetId" = $1',
            target_asset_id,
        )

        # Delete the target asset (cascades to exif, files, faces, smart_search, job_status)
        await conn.execute("DELETE FROM asset WHERE id = $1", target_asset_id)

        # Remove the mapping
        await conn.execute(
            "DELETE FROM _face_sync_asset_map WHERE target_asset_id = $1",
            target_asset_id,
        )

        await conn.execute("RELEASE SAVEPOINT delete_synced_asset")
        return True
    except Exception:
        await conn.execute("ROLLBACK TO SAVEPOINT delete_synced_asset")
        logging.getLogger(__name__).exception(
            "Failed to delete synced asset %s", target_asset_id
        )
        return False


async def delete_mirrored_person(conn, target_user_id, person_group_id) -> str:
    """Delete a mirrored person's row and its thumbnail hardlink.

    person's PK is now ("ownerId", "personGroupId") - there is no person.id.

    The guard lives in delete_target_person_in_shared_group
    (src/person_sync.py), which is where its two conditions are explained and
    tested. It is NOT the same shape as cleanup_orphaned_persons' guard, and
    an earlier version of this docstring claimed it was: this one requires a
    mapped source to still hold a person row on the group (which is what
    stops deleteEmptyGroups nulling the group's faces) and only then refuses
    on the target's own remaining faces.

    Returns "deleted", "skipped" (guard refused, or row already gone), or
    "failed".

    The thumbnail hardlink goes only once the row is confirmed deleted. That
    inverts cleanup.py's files-before-records rule, deliberately: the guard
    can refuse, and unlinking the thumbnail of a person we then keep is worse
    than the alternative failure here, which is an orphaned hardlink costing
    nothing.

    SAVEPOINT for the same reason as delete_synced_asset: the caller runs a
    whole batch inside one transaction(), and a bare except would leave the
    transaction aborted for every later item.
    """
    await conn.execute("SAVEPOINT delete_mirrored_person")
    try:
        person = await conn.fetchrow(
            'SELECT "thumbnailPath" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
            target_user_id, person_group_id,
        )
        if person is None:
            await conn.execute("RELEASE SAVEPOINT delete_mirrored_person")
            return "skipped"

        if not await delete_target_person_in_shared_group(
            conn, target_user_id, person_group_id,
        ):
            await conn.execute("RELEASE SAVEPOINT delete_mirrored_person")
            return "skipped"

        if person["thumbnailPath"]:
            remove_hardlinks([person["thumbnailPath"]])

        await conn.execute("RELEASE SAVEPOINT delete_mirrored_person")
        return "deleted"
    except Exception:
        await conn.execute("ROLLBACK TO SAVEPOINT delete_mirrored_person")
        logging.getLogger(__name__).exception(
            "Failed to delete mirrored person %s for user %s", person_group_id, target_user_id
        )
        return "failed"


async def main():
    if not ENV_FILE.exists():
        print("Error: .env not found. Copy env.example to .env and fill in your values.")
        sys.exit(1)

    from src.config import settings
    print(f"Connecting to {settings.db_hostname}:{settings.db_port}/{settings.db_database_name}")
    await init_pool()
    await ensure_tracking_tables()

    # Step 1: Choose target user
    users = await get_target_users()
    if not users:
        print("No synced assets found in _face_sync_asset_map.")
        await close_pool()
        return

    print("\nTarget users with synced assets:")
    for i, u in enumerate(users, 1):
        count = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM _face_sync_asset_map WHERE target_user_id = $1",
            u["id"],
        )
        print(f"  {i}. {u['name']} ({u['email']}) — {count['cnt']} synced assets")

    while True:
        choice = input(f"\nSelect user [1-{len(users)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(users):
                break
        except ValueError:
            pass
        print("Invalid choice, try again.")

    target_user = users[idx]
    target_user_id = target_user["id"]
    print(f"\nSelected: {target_user['name']} ({target_user['email']})")

    # Step 2: Show summary
    assets = await get_synced_assets(target_user_id)
    persons = await get_mirrored_persons(target_user_id)

    print(f"\n  Synced assets:    {len(assets)}")
    print(f"  Mirrored persons: {len(persons)}")

    if not assets and not persons:
        print("\nNothing to delete.")
        await close_pool()
        return

    # Step 3: Dry run or delete
    while True:
        action = input("\nDelete all synced data for this user? [dry-run / delete / cancel]: ").strip().lower()
        if action in ("dry-run", "delete", "cancel", "d", "c"):
            break
        print("Please enter 'dry-run', 'delete', or 'cancel'.")

    if action in ("cancel", "c"):
        print("Cancelled.")
        await close_pool()
        return

    if action == "dry-run":
        print(f"\n[DRY RUN] Would delete:")
        print(f"  {len(assets)} synced asset(s)")
        for a in assets[:20]:
            print(f"    DELETE target={a['target_asset_id']}")
        if len(assets) > 20:
            print(f"    ... and {len(assets) - 20} more")
        print(f"  {len(persons)} mirrored person(s)")
        for p in persons[:20]:
            print(f"    DELETE person_group={p['person_group_id']}")
        if len(persons) > 20:
            print(f"    ... and {len(persons) - 20} more")
        print("\nNo changes made.")
        await close_pool()
        return

    # action == "delete"
    # Phase 1: Delete all synced assets in batches
    total_assets = len(assets)
    batch_size = 200
    print(f"\nDeleting {total_assets} synced asset(s) in batches of {batch_size}...")
    deleted_assets = 0
    failed_assets = 0
    for batch_start in range(0, total_assets, batch_size):
        batch = assets[batch_start:batch_start + batch_size]
        async with transaction() as conn:
            for a in batch:
                ok = await delete_synced_asset(conn, a["target_asset_id"])
                if ok:
                    deleted_assets += 1
                else:
                    failed_assets += 1
        print(f"  Progress: {deleted_assets + failed_assets}/{total_assets} ({deleted_assets} deleted, {failed_assets} failed)")

    print(f"\nAssets: {deleted_assets} deleted, {failed_assets} failed.")

    # Phase 2: Delete all mirrored persons
    if persons:
        print(f"\nDeleting {len(persons)} mirrored person(s)...")
        deleted_persons = 0
        skipped_persons = 0
        failed_persons = 0
        async with transaction() as conn:
            for p in persons:
                result = await delete_mirrored_person(conn, target_user_id, p["person_group_id"])
                if result == "deleted":
                    deleted_persons += 1
                elif result == "skipped":
                    skipped_persons += 1
                else:
                    failed_persons += 1
        print(f"Persons: {deleted_persons} deleted, {skipped_persons} skipped (faces still assigned), {failed_persons} failed.")

    print("\nDone.")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
