#!/usr/bin/env python3
"""
Manual test script that runs the sidecar sync logic once against a local Immich instance.
Must be run inside a Docker container on the Immich network (see run-utility.sh).

Usage:
  ./run-utility.sh test_sync.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from src.env_bootstrap import bootstrap

if not bootstrap():
    print("Error: .env not found. Copy env.example to .env and fill in your values.")
    sys.exit(1)

# Point CONFIG_FILE at local config.yaml if it exists
config_yaml = Path(__file__).parent / "config.yaml"
if config_yaml.is_file():
    os.environ.setdefault("CONFIG_FILE", str(config_yaml))

os.environ.setdefault("LOG_LEVEL", "DEBUG")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.config import settings
from src.db import init_pool, close_pool
from src.main import ensure_tracking_tables
from src.sync_engine import run_full_sync


async def _source_face_health(source_user_ids):
    """Count each source user's own faces, and how many are unassigned.

    This is the canary for the one failure mode where the sidecar damages the
    account it is only supposed to read from. Immich's ``deleteEmptyGroups``
    removes any ``person_group`` with no ``person`` row, and
    ``asset_face."personGroupId"`` is ON DELETE SET NULL — so if the sidecar
    ever deletes the last person row on a shared group, every face in that
    group is silently unassigned, including the source user's faces on the
    source user's own photos.

    Nothing else printed by this script would show that: the target side would
    look perfectly healthy. Sample before and after the cycle and compare.
    """
    from src.db import fetch_all

    return {
        row["ownerId"]: (row["unassigned"], row["total"])
        for row in await fetch_all(
            """
            SELECT a."ownerId",
                   COUNT(*) FILTER (WHERE af."personGroupId" IS NULL) AS unassigned,
                   COUNT(*) AS total
            FROM asset_face af
            JOIN asset a ON a.id = af."assetId"
            WHERE a."ownerId" = ANY($1)
              AND af."deletedAt" IS NULL
              AND a."deletedAt" IS NULL
            GROUP BY a."ownerId"
            """,
            list(source_user_ids),
        )
    }


async def main():
    # Use first sync job's target user for verification queries
    if not settings.sync_jobs:
        print("Error: No sync jobs configured. Check your config.yaml or .env.")
        sys.exit(1)
    target_user_id = settings.sync_jobs[0].target_user_id
    source_user_ids = {job.source_user_id for job in settings.sync_jobs}

    print("=== Initializing database pool ===")
    await init_pool()

    print("\n=== Ensuring tracking tables ===")
    await ensure_tracking_tables()

    print("\n=== Source face health (before) ===")
    before = await _source_face_health(source_user_ids)
    for uid, (unassigned, total) in sorted(before.items(), key=lambda kv: str(kv[0])):
        print(f"  source {uid}: {unassigned} unassigned of {total} faces")
    if not before:
        print("  (no source faces found — nothing to compare against)")

    print("\n=== Running full sync ===")
    stats = await run_full_sync()
    print(f"\n=== Sync results: {stats} ===")

    print("\n=== Source face health (after) ===")
    after = await _source_face_health(source_user_ids)
    regressed = False
    for uid in sorted(set(before) | set(after), key=str):
        was_unassigned, was_total = before.get(uid, (0, 0))
        now_unassigned, now_total = after.get(uid, (0, 0))
        delta = now_unassigned - was_unassigned
        print(
            f"  source {uid}: {now_unassigned} unassigned of {now_total} faces "
            f"(was {was_unassigned} of {was_total}, delta {delta:+d})"
        )
        if delta > 0:
            regressed = True

    if regressed:
        print(
            "\n  *** STOP: a source user's own faces lost their person assignment. ***\n"
            "  The sidecar must never write to the source account. This is the\n"
            "  signature of a shared person_group being emptied — Immich's\n"
            "  deleteEmptyGroups then nulls every face in that group.\n"
            "  Do not run another cycle. Restore from your pre-run pg_dump and\n"
            "  report which statement ran, from the DEBUG log above."
        )
    else:
        print("\n  OK — no source faces lost their person assignment.")

    # Verify results
    from src.db import fetch_all, fetch_one
    print("\n=== Verification ===")

    target_assets = await fetch_all(
        "SELECT id, \"originalPath\", \"originalFileName\" FROM asset WHERE \"ownerId\" = $1 AND \"deletedAt\" IS NULL",
        target_user_id,
    )
    print(f"\nTarget user's assets: {len(target_assets)}")
    for a in target_assets:
        print(f"  {a['id']} — {a['originalFileName']} — {a['originalPath']}")

    target_faces = await fetch_all("""
        SELECT af.id, af."assetId", af."personGroupId", p.name as person_name
        FROM asset_face af
        JOIN asset a ON a.id = af."assetId"
        LEFT JOIN person p ON p."personGroupId" = af."personGroupId" AND p."ownerId" = a."ownerId"
        WHERE a."ownerId" = $1 AND af."deletedAt" IS NULL
    """, target_user_id)
    print(f"\nTarget user's faces: {len(target_faces)}")
    for f in target_faces:
        print(f"  face={f['id']} asset={f['assetId']} person_group={f['personGroupId']} name={f['person_name']}")

    target_persons = await fetch_all(
        "SELECT \"personGroupId\", name FROM person WHERE \"ownerId\" = $1",
        target_user_id,
    )
    print(f"\nTarget user's persons: {len(target_persons)}")
    for p in target_persons:
        print(f"  group={p['personGroupId']} — name='{p['name']}'")

    smart_count = await fetch_one("""
        SELECT COUNT(*) as cnt FROM smart_search ss
        JOIN asset a ON a.id = ss."assetId"
        WHERE a."ownerId" = $1 AND a."deletedAt" IS NULL
    """, target_user_id)
    print(f"\nTarget user's smart_search entries: {smart_count['cnt']}")

    mappings = await fetch_all("SELECT * FROM _face_sync_asset_map")
    print(f"\nAsset mappings: {len(mappings)}")
    for m in mappings:
        print(f"  {m['source_asset_id']} -> {m['target_asset_id']}")

    # v3.2.0 cluster-group port: there is no longer a sidecar-owned person
    # mapping table — person identity is shared directly via Immich's
    # person_group table. Show that invariant instead: a person_group with
    # person rows owned by more than one user is a group the sidecar has
    # linked across the source/target pair.
    shared_groups = await fetch_all("""
        SELECT "personGroupId", COUNT(DISTINCT "ownerId") AS owners
        FROM person
        GROUP BY "personGroupId"
        HAVING COUNT(DISTINCT "ownerId") > 1
    """)
    print(f"\nShared person groups (identity linked across users): {len(shared_groups)}")
    for g in shared_groups:
        print(f"  group={g['personGroupId']} shared by {g['owners']} users")

    await close_pool()

    if regressed:
        # Repeated, because the banner above is now thousands of lines up the
        # scrollback, and returned so the exit status carries it too. The
        # verification dump is left in deliberately: if this has fired, it is
        # the evidence you want.
        print(
            "\n=== FAILED: a source user's own faces lost their person assignment ==="
            "\n    Scroll up to the face-health section. Do not run another cycle."
        )
    else:
        print("\n=== Done ===")
    return regressed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
