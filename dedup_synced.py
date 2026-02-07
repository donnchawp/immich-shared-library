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
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

# Load .env file
env_file = Path(__file__).parent / ".env"
if not env_file.exists():
    print("Error: .env not found. Copy env.example to .env and fill in your values.")
    sys.exit(1)

for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, _, value = line.partition("=")
    if key and value:
        os.environ.setdefault(key.strip(), value.strip())

os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.db import init_pool, close_pool, fetch_all, fetch_one
from src.file_ops import remove_hardlinks
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


async def delete_synced_asset(conn, source_asset_id, target_asset_id) -> bool:
    """Delete a synced target asset and record the source as skipped.

    Follows the same pattern as cleanup_deleted_assets:
    remove hardlinks -> delete album_asset -> delete asset (cascades) -> delete mapping -> record skip.
    """
    try:
        # Get file paths before deleting records
        files = await conn.fetch(
            'SELECT path FROM asset_file WHERE "assetId" = $1',
            target_asset_id,
        )
        file_paths = [f["path"] for f in files]

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

        # Record the source asset as skipped so the sync engine won't recreate it
        await conn.execute(
            """
            INSERT INTO _face_sync_skipped (source_asset_id, reason)
            VALUES ($1, 'duplicate_filename')
            ON CONFLICT (source_asset_id) DO NOTHING
            """,
            source_asset_id,
        )

        return True
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to delete synced asset %s", target_asset_id
        )
        return False


async def main(match_time: bool = False):
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
    while True:
        action = input("Delete these synced copies? [dry-run / delete / cancel]: ").strip().lower()
        if action in ("dry-run", "delete", "cancel", "d", "c"):
            break
        print("Please enter 'dry-run', 'delete', or 'cancel'.")

    if action in ("cancel", "c"):
        print("Cancelled.")
        await close_pool()
        return

    if action == "dry-run":
        print(f"\n[DRY RUN] Would delete {len(duplicates)} synced asset(s):")
        for d in duplicates:
            print(f"  DELETE target={d['target_asset_id']}  ({d['synced_filename']})")
            print(f"    SKIP source={d['source_asset_id']} (record in _face_sync_skipped)")
        print("\nNo changes made.")
        await close_pool()
        return

    # action == "delete"
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
                ok = await delete_synced_asset(conn, d["source_asset_id"], d["target_asset_id"])
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
