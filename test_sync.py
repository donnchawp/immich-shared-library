#!/usr/bin/env python3
"""
Manual test script that runs the sidecar sync logic once against a local Immich instance.
Connects to Postgres via localhost (requires port 5432 exposed or SSH tunnel).

Usage:
  # First expose postgres: docker compose -f docker-compose.yml exec -d database sh -c 'echo done'
  # Or add ports: ['5432:5432'] to the database service temporarily
  # Then:
  python3 test_sync.py
"""
import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("DB_HOSTNAME", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_USERNAME", "postgres")
os.environ.setdefault("DB_PASSWORD", "postgres")
os.environ.setdefault("DB_DATABASE_NAME", "immich")
os.environ.setdefault("IMMICH_API_URL", "http://localhost:2283")
os.environ.setdefault("IMMICH_API_KEY", "cFNp4adkfYyMAnmz1IiVCBCSLPCD4oTbmDiMxJaYo")
os.environ.setdefault("SOURCE_USER_ID", "c7e0a257-c493-4f11-8672-9788e2c45943")
os.environ.setdefault("TARGET_USER_ID", "9d3f2dc3-ac51-41b6-bf27-9625f7a892c7")
os.environ.setdefault("TARGET_LIBRARY_ID", "96fe97d6-2b3a-48fd-810b-265dc190b39d")
os.environ.setdefault("SHARED_PATH_PREFIX", "/external_library/donncha/")
os.environ.setdefault("TARGET_PATH_PREFIX", "/external_library/jacinta/")
os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")
os.environ.setdefault("SCAN_INTERVAL_SECONDS", "9999")
os.environ.setdefault("LOG_LEVEL", "DEBUG")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.db import init_pool, close_pool
from src.main import ensure_tracking_tables
from src.sync_engine import run_full_sync


async def main():
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
        __import__('uuid').UUID("9d3f2dc3-ac51-41b6-bf27-9625f7a892c7"),
    )
    print(f"\nJacinta's assets: {len(target_assets)}")
    for a in target_assets:
        print(f"  {a['id']} — {a['originalFileName']} — {a['originalPath']}")

    target_faces = await fetch_all("""
        SELECT af.id, af."assetId", af."personId", p.name as person_name
        FROM asset_face af
        JOIN asset a ON a.id = af."assetId"
        LEFT JOIN person p ON p.id = af."personId"
        WHERE a."ownerId" = $1 AND af."deletedAt" IS NULL
    """, __import__('uuid').UUID("9d3f2dc3-ac51-41b6-bf27-9625f7a892c7"))
    print(f"\nJacinta's faces: {len(target_faces)}")
    for f in target_faces:
        print(f"  face={f['id']} asset={f['assetId']} person={f['personId']} name={f['person_name']}")

    target_persons = await fetch_all(
        "SELECT id, name FROM person WHERE \"ownerId\" = $1",
        __import__('uuid').UUID("9d3f2dc3-ac51-41b6-bf27-9625f7a892c7"),
    )
    print(f"\nJacinta's persons: {len(target_persons)}")
    for p in target_persons:
        print(f"  {p['id']} — name='{p['name']}'")

    smart_count = await fetch_one("""
        SELECT COUNT(*) as cnt FROM smart_search ss
        JOIN asset a ON a.id = ss."assetId"
        WHERE a."ownerId" = $1 AND a."deletedAt" IS NULL
    """, __import__('uuid').UUID("9d3f2dc3-ac51-41b6-bf27-9625f7a892c7"))
    print(f"\nJacinta's smart_search entries: {smart_count['cnt']}")

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
