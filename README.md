# Immich Shared Library

A Docker sidecar service that syncs a subset of [Immich](https://immich.app/) photos from one user to another — without duplicating ML processing.

## The Problem

Immich's partner sharing is all-or-nothing: you share your entire library or nothing. The common workaround (symlinking external libraries) makes both users' assets visible, but Immich runs face detection, face recognition, and CLIP embedding for each user independently — doubling all ML work.

## How It Works

This sidecar connects directly to Immich's PostgreSQL database and, for each shared source asset:

1. Creates a target asset record with remapped file paths
2. Copies EXIF metadata, CLIP embeddings, face detection results, and face recognition data
3. Hardlinks thumbnail/preview files (zero extra disk space)
4. Creates mirrored person records with hardlinked face thumbnails

Because Immich skips ML processing for assets that already have results, the target user's assets appear instantly with full search, face recognition, and timeline support — no ML queue, no GPU time.

The sidecar runs continuously, syncing new assets, propagating person name changes, and cleaning up deletions.

## Prerequisites

- A running Immich instance (v2+) with Docker Compose
- Two Immich users (source and target)
- An external library for the source user containing the photos to share
- An external library for the target user with symlinks pointing to the same photos
- The source user's photos must be fully processed by Immich (metadata, faces, CLIP)

## Installation

### 1. Create an API key

In Immich, go to **Account Settings > API Keys** and create a key. This key needs permission to trigger library scans.

### 2. Set up the target user's external library

Create an external library for the target user in Immich. The library's import path should contain symlinks pointing to the source user's photos. For example:

```
/external_library/
  user_a/
    shared/         # Photos to share with user_b
      photo1.jpg
      photo2.jpg
    private/        # Not shared
      photo3.jpg
  user_b/
    shared -> ../user_a/shared   # Single symlink to the entire directory
```

### 3. Get the required UUIDs

You need three UUIDs from Immich: the source user ID, target user ID, and target user's external library ID.

**User IDs:** In the Immich web UI, go to **Administration > Users** and click on a username. The UUID appears in the URL: `/admin/users/{UUID}`.

**Library ID:** In the Immich web UI, go to **Administration > External Libraries** and click on the target user's external library. The UUID appears in the URL: `/admin/library-management/{UUID}`.

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
DB_PASSWORD=postgres

# Host paths to Immich's data directories
IMMICH_DATA_DIR=../immich-app/library
EXTERNAL_LIBRARY_DIR=../immich-app/external_library

IMMICH_API_KEY=your-immich-api-key

SOURCE_USER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TARGET_USER_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
TARGET_LIBRARY_ID=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz

SHARED_PATH_PREFIX=/external_library/user_a/shared/
TARGET_PATH_PREFIX=/external_library/user_b/shared/
```

### 5. Disable Immich's global auto-scan

In Immich's **Administration > Settings > External Library**, disable the periodic scan: "Library Watching" and "Periodic Scanning". The sidecar takes over scanning for all libraries except the target library, which should only receive pre-populated assets.

### 6. Start the sidecar

The sidecar runs as a standalone compose project that connects to Immich's Docker network. From this directory:

```bash
docker compose up -d
```

The `IMMICH_DATA_DIR` and `EXTERNAL_LIBRARY_DIR` in `.env` must point to the same host directories that Immich mounts (typically `./library` and `./external_library` in your Immich directory). Hardlinks require the same filesystem.

The sidecar will:
- Wait for the Immich server to become available
- Create its tracking tables (`_face_sync_asset_map`, `_face_sync_person_map`)
- Run a sync cycle every 60 seconds (configurable via `SYNC_INTERVAL_SECONDS`)
- Trigger library scans every 300 seconds (configurable via `SCAN_INTERVAL_SECONDS`)

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `IMMICH_DATA_DIR` | *(required)* | Host path to Immich's upload/data directory (e.g., `../immich-app/library`) |
| `EXTERNAL_LIBRARY_DIR` | *(required)* | Host path to the external library directory (e.g., `../immich-app/external_library`) |
| `DB_PASSWORD` | `postgres` | PostgreSQL password (same as Immich) |
| `DB_USERNAME` | `postgres` | PostgreSQL username |
| `DB_DATABASE_NAME` | `immich` | PostgreSQL database name |
| `IMMICH_API_KEY` | *(required)* | Immich API key |
| `SOURCE_USER_ID` | *(required)* | UUID of the source user |
| `TARGET_USER_ID` | *(required)* | UUID of the target user |
| `TARGET_LIBRARY_ID` | *(required)* | UUID of the target user's external library |
| `SHARED_PATH_PREFIX` | *(required)* | Path prefix for source assets (e.g., `/external_library/user_a/shared/`) |
| `TARGET_PATH_PREFIX` | | Path prefix for target assets (e.g., `/external_library/user_b/shared/`) |
| `SYNC_INTERVAL_SECONDS` | `60` | Seconds between sync cycles |
| `SCAN_INTERVAL_SECONDS` | `300` | Seconds between library scans |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## How the Sync Works

Each sync cycle runs four phases:

1. **New assets** — Finds fully-processed source assets not yet synced. Creates target asset records with copied EXIF, CLIP embeddings, faces, and hardlinked thumbnails.
2. **Incremental faces** — Detects face updates on already-synced assets (using a watermark timestamp) and copies new faces.
3. **Person metadata** — Syncs person name changes, visibility (`isHidden`), and thumbnail updates from source to target.
4. **Cleanup** — Removes target assets whose source was deleted or trashed. Detects person merges (face reassignment). Removes orphaned target persons.

## Caveats

- **`force=true` jobs**: If someone triggers a force re-process in Immich, it will re-run ML on the target user's assets, overwriting the copied data. The sidecar will re-sync on the next cycle, but there will be temporary GPU usage.
- **Same filesystem required**: Hardlinks only work when the sidecar container mounts the same volume as Immich. Cross-filesystem setups would need file copies instead.
- **Direct database access**: This service writes directly to Immich's database. Immich schema changes in future versions may require updates to this sidecar.
- **Single direction**: Sync is one-way (source → target). Changes made to target assets in Immich are not propagated back.

## Contributing

### Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Running Against a Local Immich Instance

The test script `test_sync.py` runs a single sync cycle with hardcoded config. Since the Immich PostgreSQL container doesn't expose port 5432 by default, tests must run inside a Docker container on the Immich network:

```bash
docker run --rm --network immich_default \
  -v $(pwd):/app \
  -v /path/to/immich-app/library:/data \
  -v /path/to/immich-app/external_library:/external_library \
  -e DB_HOSTNAME=<postgres-container-ip> \
  -e DB_PORT=5432 -e DB_USERNAME=postgres -e DB_PASSWORD=postgres \
  -e DB_DATABASE_NAME=immich \
  -e IMMICH_API_URL=http://immich_server:2283 \
  -e IMMICH_API_KEY=<your-key> \
  -e SOURCE_USER_ID=<uuid> -e TARGET_USER_ID=<uuid> \
  -e TARGET_LIBRARY_ID=<uuid> \
  -e SHARED_PATH_PREFIX=/external_library/source_user/ \
  -e TARGET_PATH_PREFIX=/external_library/target_user/ \
  -e LOG_LEVEL=DEBUG \
  -w /app python:3.12-slim \
  bash -c 'pip install asyncpg httpx pydantic pydantic-settings && python test_sync.py'
```

Find the Postgres container IP with:

```bash
docker inspect immich_postgres --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
```

### Project Structure

```
src/
  main.py          — Entry point: config validation, health check, concurrent loops
  sync_engine.py   — Orchestrates 4-phase sync cycle
  asset_sync.py    — Asset record creation, EXIF copy, path remapping
  ml_sync.py       — Face and embedding sync
  person_sync.py   — Person mirroring, name/visibility sync
  cleanup.py       — Deletion detection and cleanup
  file_ops.py      — Hardlink creation and removal
  db.py            — asyncpg connection pool and transaction helpers
  config.py        — Pydantic Settings for environment variables
  immich_api.py    — Immich REST API client
  scan_manager.py  — Selective library scanning
  health.py        — TCP health check server
```

### Key Things to Know

- Immich tables are **singular** (`asset`, not `assets`) with **camelCase** columns that must be double-quoted in SQL.
- The sidecar creates two tracking tables prefixed with `_face_sync_` to avoid colliding with Immich's schema.
- Each asset sync uses a PostgreSQL SAVEPOINT so one failure doesn't roll back the entire batch.
- Cleanup deletes hardlinked files before DB records to avoid orphan files on crash.
