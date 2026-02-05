# Immich Shared Library

A Docker sidecar service that syncs a subset of [Immich](https://immich.app/) photos from one user to another — without duplicating ML processing.

## The Problem

Immich's partner sharing is all-or-nothing: you share your entire library or nothing. The common workaround is symlinking an external library directory so both users point at the same photos. This works — both users see the photos — but Immich processes each user's assets independently through the full ML pipeline:

| Per-asset work | Symlink only | With sidecar |
|---|---|---|
| Metadata extraction (EXIF) | 2x | 1x (copied) |
| Thumbnail generation | 2x | 1x (hardlinked) |
| CLIP embedding (smart search) | 2x (GPU) | 1x (copied) |
| Face detection | 2x (GPU) | 1x (copied) |
| Face recognition | 2x (GPU) | 1x (copied) |
| Person clustering | Independent per user | Mirrored from source |
| Person names | Must name separately | Synced automatically |

With 1,000 shared photos, the symlink approach queues 5,000+ extra ML jobs (metadata, thumbnails, CLIP, face detection, face recognition) that produce identical results. Each user also gets independent person clusters, so you'd need to name each person twice — and the clusters may group faces differently.

## How It Works

This sidecar connects directly to Immich's PostgreSQL database and, for each shared source asset:

1. Creates a target asset record with remapped file paths
2. Copies EXIF metadata, CLIP embeddings, face detection results, and face recognition data
3. Hardlinks thumbnail and preview files (zero extra disk space)
4. Creates mirrored person records with hardlinked face thumbnails
5. Pre-populates job status so Immich skips all ML processing for these assets

The target user's assets appear instantly with full search, face recognition, and timeline support — no ML queue, no GPU time. The sidecar runs continuously, syncing new assets, propagating person name changes, and cleaning up deletions.

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
UPLOAD_LOCATION=../immich-app/library
EXTERNAL_LIBRARY_DIR=../immich-app/external_library

IMMICH_API_KEY=your-immich-api-key

SOURCE_USER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TARGET_USER_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
TARGET_LIBRARY_ID=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz

# Path prefixes as seen inside the Immich container (not on the host)
SHARED_PATH_PREFIX=/external_library/user_a/shared/
TARGET_PATH_PREFIX=/external_library/user_b/shared/
```

### 5. Configure the target external library

The target user's external library contains symlinks to the source user's shared photos. Immich must **not** scan or watch this library — if it does, it will create its own asset records and run ML processing independently, defeating the purpose of the sidecar.

Add an exclusion pattern to the target library that tells Immich to ignore all files:

1. Go to **Administration > External Libraries**
2. Click on the target user's external library
3. Add `**/*` as an exclusion pattern and save

The sidecar bypasses library scanning entirely by writing asset records directly to the database. Immich sees the target user's assets because they exist in the `asset` table, not because it scanned the filesystem.

### 6. Enable library watching

In Immich's **Administration > Settings > External Library**, enable **Library Watching**. This uses filesystem events (inotify) to detect new files added to any external library. Because the target library has the `**/*` exclusion pattern, Immich will ignore file events in that library.

When a new file is added to the source user's external library, Immich will automatically detect it, create an asset record, and run ML processing (metadata extraction, face detection, CLIP embedding). Once processing completes, the sidecar picks it up on the next sync cycle.

> **Note:** The sidecar does not trigger library scans itself. It relies on Immich's library watching (or manual scans) to discover new source files. If library watching doesn't work in your environment (e.g., network drives), you can trigger scans manually after adding files:
> ```bash
> curl -X POST "http://localhost:2283/api/libraries/SOURCE_LIBRARY_ID/scan" \
>   -H "x-api-key: YOUR_KEY"
> ```

### 7. Start the sidecar

The sidecar runs as a standalone compose project that connects to Immich's Docker network. From this directory:

```bash
docker compose up -d
```

The `UPLOAD_LOCATION` and `EXTERNAL_LIBRARY_DIR` in `.env` must point to the same host directories that Immich mounts (typically `./library` and `./external_library` in your Immich directory). Hardlinks require the same filesystem.

The sidecar will:
- Wait for the Immich server to become available
- Create its tracking tables (`_face_sync_asset_map`, `_face_sync_person_map`)
- Run a sync cycle every 60 seconds (configurable via `SYNC_INTERVAL_SECONDS`)

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `UPLOAD_LOCATION` | *(required)* | Host path to Immich's upload/data directory (e.g., `../immich-app/library`) |
| `EXTERNAL_LIBRARY_DIR` | *(required)* | Host path to the external library directory (e.g., `../immich-app/external_library`) |
| `DB_PASSWORD` | `postgres` | PostgreSQL password (same as Immich) |
| `DB_USERNAME` | `postgres` | PostgreSQL username |
| `DB_DATABASE_NAME` | `immich` | PostgreSQL database name |
| `IMMICH_API_KEY` | *(required)* | Immich API key |
| `SOURCE_USER_ID` | *(required)* | UUID of the source user |
| `TARGET_USER_ID` | *(required)* | UUID of the target user |
| `TARGET_LIBRARY_ID` | *(required)* | UUID of the target user's external library |
| `SHARED_PATH_PREFIX` | *(required)* | Path prefix for source assets as seen inside the Immich container (e.g., `/external_library/user_a/shared/`) |
| `TARGET_PATH_PREFIX` | | Path prefix for target assets as seen inside the Immich container (e.g., `/external_library/user_b/shared/`) |
| `SYNC_INTERVAL_SECONDS` | `60` | Seconds between sync cycles |
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

The test script `test_sync.py` runs a single sync cycle against a local Immich instance. It reads configuration from a `test.env` file:

```bash
cp test.env.example test.env
# Edit test.env with your Immich API key, user IDs, and library ID
```

Since the Immich PostgreSQL container doesn't expose port 5432 by default, tests must run inside a Docker container on the Immich network:

```bash
docker run --rm --network immich_default \
  -v $(pwd):/app \
  -v /path/to/immich-app/library:/data \
  -v /path/to/immich-app/external_library:/external_library \
  -w /app python:3.12-slim \
  bash -c 'pip install asyncpg httpx pydantic pydantic-settings && python test_sync.py'
```

Update `DB_HOSTNAME` in `test.env` to point to the Postgres container IP:

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
  immich_api.py    — Immich REST API client (health check)
  health.py        — TCP health check server
```

### Key Things to Know

- Immich tables are **singular** (`asset`, not `assets`) with **camelCase** columns that must be double-quoted in SQL.
- The sidecar creates two tracking tables prefixed with `_face_sync_` to avoid colliding with Immich's schema.
- Each asset sync uses a PostgreSQL SAVEPOINT so one failure doesn't roll back the entire batch.
- Cleanup deletes hardlinked files before DB records to avoid orphan files on crash.
