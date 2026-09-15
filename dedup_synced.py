#!/usr/bin/env python3
"""
Detect and remove synced assets that duplicate the target user's own uploads.

Matches by filename stem (without extension) + EXIF dateTimeOriginal.
Deletes the synced copy, leaving the user's own upload intact.

Usage:
  python3 dedup_synced.py [--match-time]
"""
import argparse
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from src.env_bootstrap import ENV_FILE, bootstrap

# Deferred rather than exiting here, matching delete_synced.py: importing this
# module must stay side-effect-free enough for the test suite to reach the
# functions in it. main() does the check before it touches the database.
bootstrap()

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src import confirm
from src.asset_sync import record_skipped_duplicates
from src.cleanup import delete_target_asset
from src.db import init_pool, close_pool, fetch_all, fetch_one
from src.main import ensure_tracking_tables


async def get_target_users() -> list[dict]:
    """Get distinct target users from the asset mapping table, with email for display."""
    rows = await fetch_all("""
        SELECT DISTINCT m.target_user_id, u.email, u.name
        FROM _face_sync_asset_map m
        JOIN "user" u ON u.id = m.target_user_id
        ORDER BY u.email
    """)
    return [{"id": r["target_user_id"], "email": r["email"], "name": r["name"]} for r in rows]


async def find_duplicates(target_user_id, *, match_time: bool = False) -> list[dict]:
    """Find synced target assets that duplicate the user's own uploads by stem + dateTimeOriginal.

    When match_time is True, compares the full timestamp (to the second) instead of just the date.
    Timezone differences are normalised by converting to wall-clock time using the stored timeZone
    column: photos with TZ info get converted back to local time, photos without TZ info are
    treated as already being in local time.
    """
    if match_time:
        time_clause = """
            AND date_trunc('second', te."dateTimeOriginal" AT TIME ZONE COALESCE(te."timeZone", 'UTC'))
              = date_trunc('second', oe."dateTimeOriginal" AT TIME ZONE COALESCE(oe."timeZone", 'UTC'))"""
    else:
        time_clause = """
            AND oe."dateTimeOriginal"::date = te."dateTimeOriginal"::date"""

    rows = await fetch_all(f"""
        SELECT
            m.source_asset_id,
            m.target_asset_id,
            ta."originalFileName" AS synced_filename,
            ta."originalPath" AS synced_path,
            te."dateTimeOriginal" AS capture_date,
            oa.id AS original_asset_id,
            oa."originalFileName" AS original_filename,
            oa."originalPath" AS original_path
        FROM _face_sync_asset_map m
        JOIN asset ta ON ta.id = m.target_asset_id AND ta."deletedAt" IS NULL
        JOIN asset_exif te ON te."assetId" = ta.id AND te."dateTimeOriginal" IS NOT NULL
        JOIN asset oa ON oa."ownerId" = m.target_user_id
            AND oa.id != ta.id
            AND oa."libraryId" IS DISTINCT FROM ta."libraryId"
            AND oa."deletedAt" IS NULL
        JOIN asset_exif oe ON oe."assetId" = oa.id
            {time_clause}
        WHERE m.target_user_id = $1
          AND regexp_replace(ta."originalFileName", '\\.[^.]+$', '') =
              regexp_replace(oa."originalFileName", '\\.[^.]+$', '')
        ORDER BY te."dateTimeOriginal"
    """, target_user_id)
    return [dict(r) for r in rows]


async def delete_synced_asset(conn, source_asset_id, target_asset_id, target_user_id) -> bool:
    """Delete a synced target asset and record the source as skipped.

    The deletion itself is cleanup.delete_target_asset -- the same one the sync
    engine and delete_synced.py use. This file used to carry its own copy,
    which selected every asset_file path and handed the XMP sidecar to
    remove_hardlinks; the target's sidecar path resolves through the
    external-library symlink to the SOURCE user's own file.

    The skip record is asset_sync.record_skipped_duplicates, for the same
    reason: this file's own INSERT named only source_asset_id, and
    _face_sync_skipped is keyed on (source_asset_id, target_user_id) with
    target_user_id NOT NULL.

    The two halves are deliberately not one atomic unit. delete_target_asset
    unlinks thumbnails before deleting the rows that name them and the
    filesystem does not roll back, so rolling the delete back on a failed skip
    record would restore an asset whose files are gone. Losing only the skip
    record is recoverable: the sync engine recreates the asset next cycle and
    the tool finds it again, which is the state the user was in before running
    it. So the delete stands, and the INSERT gets its own savepoint to keep a
    failure off the batch.
    """
    if not await delete_target_asset(conn, target_asset_id):
        return False

    try:
        async with conn.transaction():
            await record_skipped_duplicates(conn, {source_asset_id}, target_user_id)
        return True
    except Exception:
        logging.getLogger(__name__).exception(
            "Deleted asset %s but failed to record source %s as skipped",
            target_asset_id, source_asset_id,
        )
        return False


async def main(match_time: bool = False):
    if not ENV_FILE.exists():
        print("Error: .env not found. Copy env.example to .env and fill in your values.")
        sys.exit(1)

    from src.config import settings
    print(f"Connecting to {settings.db_hostname}:{settings.db_port}/{settings.db_database_name}")
    if match_time:
        print("Time matching enabled (comparing full timestamp with TZ normalisation)")
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
    print(f"\nChecking {target_user['name']} ({target_user['email']})...")

    # Step 2: Find duplicates
    duplicates = await find_duplicates(target_user["id"], match_time=match_time)
    if not duplicates:
        print("No duplicates found. All synced assets are unique.")
        await close_pool()
        return

    print(f"\nFound {len(duplicates)} synced asset(s) that duplicate existing uploads:\n")
    for d in duplicates:
        date_str = d["capture_date"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {date_str}  {d['synced_filename']}")
        print(f"    Synced:   {d['synced_path']}")
        print(f"    Original: {d['original_path']}")
        print()

    # Step 3: Dry run or delete
    action = confirm.ask("Delete these synced copies?")

    if action == confirm.CANCEL:
        print("Cancelled.")
        await close_pool()
        return

    if action == confirm.DRY_RUN:
        print(f"\n[DRY RUN] Would delete {len(duplicates)} synced asset(s):")
        for d in duplicates:
            print(f"  DELETE target={d['target_asset_id']}  ({d['synced_filename']})")
            print(f"    SKIP source={d['source_asset_id']} (record in _face_sync_skipped)")
        print("\nNo changes made.")
        await close_pool()
        return

    # action == confirm.DELETE
    total = len(duplicates)
    batch_size = 200
    print(f"\nDeleting {total} synced asset(s) in batches of {batch_size}...")
    from src.db import transaction
    deleted = 0
    failed = 0
    for batch_start in range(0, total, batch_size):
        batch = duplicates[batch_start:batch_start + batch_size]
        async with transaction() as conn:
            for d in batch:
                ok = await delete_synced_asset(
                    conn, d["source_asset_id"], d["target_asset_id"], target_user["id"],
                )
                if ok:
                    deleted += 1
                else:
                    failed += 1
        print(f"  Progress: {deleted + failed}/{total} ({deleted} deleted, {failed} failed)")

    print(f"\nDone: {deleted} deleted, {failed} failed.")
    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect and remove synced assets that duplicate the target user's own uploads.")
    parser.add_argument("--match-time", action="store_true",
                        help="Compare full capture time (not just date), with timezone normalisation")
    args = parser.parse_args()
    asyncio.run(main(match_time=args.match_time))
