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

# Point CONFIG_FILE at local config.yaml if it exists
config_yaml = Path(__file__).parent / "config.yaml"
if config_yaml.is_file():
    os.environ.setdefault("CONFIG_FILE", str(config_yaml))

os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")
os.environ.setdefault("LOG_LEVEL", "DEBUG")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.config import settings
from src.db import init_pool, close_pool
from src.main import ensure_tracking_tables
from src.sync_engine import run_full_sync


async def main():
    # Use first sync job's target user for verification queries
    if not settings.sync_jobs:
        print("Error: No sync jobs configured. Check your config.yaml or .env.")
        sys.exit(1)
    target_user_id = settings.sync_jobs[0].target_user_id

    print("=== Initializing database pool ===")
    await init_pool()

    print("\n=== Ensuring tracking tables ===")
    await ensure_tracking_tables()

    print("\n=== Running full sync ===")
    stats = await run_full_sync()
    print(f"\n=== Sync results: {stats} ===")

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
    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
