#!/usr/bin/env python3
"""
Manual test script that runs the sidecar sync logic once against a local Immich instance.
Connects to Postgres via localhost (requires port 5432 exposed or SSH tunnel).

Usage:
  cp test.env.example test.env   # then fill in your values
  python3 test_sync.py
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

# Load test.env file
env_file = Path(__file__).parent / "test.env"
if not env_file.exists():
    print("Error: test.env not found. Copy test.env.example to test.env and fill in your values.")
    sys.exit(1)

for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, _, value = line.partition("=")
    if key and value:
        os.environ.setdefault(key.strip(), value.strip())

os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")
os.environ.setdefault("LOG_LEVEL", "DEBUG")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.db import init_pool, close_pool
from src.main import ensure_tracking_tables
from src.sync_engine import run_full_sync


async def main():
    target_user_id = UUID(os.environ["TARGET_USER_ID"])

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
        SELECT af.id, af."assetId", af."personId", p.name as person_name
        FROM asset_face af
        JOIN asset a ON a.id = af."assetId"
        LEFT JOIN person p ON p.id = af."personId"
        WHERE a."ownerId" = $1 AND af."deletedAt" IS NULL
    """, target_user_id)
    print(f"\nTarget user's faces: {len(target_faces)}")
    for f in target_faces:
        print(f"  face={f['id']} asset={f['assetId']} person={f['personId']} name={f['person_name']}")

    target_persons = await fetch_all(
        "SELECT id, name FROM person WHERE \"ownerId\" = $1",
        target_user_id,
    )
    print(f"\nTarget user's persons: {len(target_persons)}")
    for p in target_persons:
        print(f"  {p['id']} — name='{p['name']}'")

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

    person_mappings = await fetch_all("SELECT * FROM _face_sync_person_map")
    print(f"\nPerson mappings: {len(person_mappings)}")
    for m in person_mappings:
        print(f"  {m['source_person_id']} -> {m['target_person_id']}")

    await close_pool()
    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
