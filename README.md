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
- Two or more Immich users (at least one source and one target)
- Source assets must be fully processed by Immich (metadata, faces, CLIP)

The sidecar supports two sync methods. You can use one or both:

| Method | What it syncs | Source |
|---|---|---|
| **External Library Sync** | A subset (or all) of one user's external library | Source user's external library directory |
| **Upload Sync** | App/website uploads from a different user | Source user's upload directory (camera roll, web uploads, etc.) |

### Example scenario

Alice manages the family photo library in Lightroom Classic and imports the finished edits into Immich via an external library. She uses **External Library Sync** to share a curated subset of family photos with Bob — just the family shots, not the street photography or landscapes.

Bob uploads all his phone photos through the Immich app. Alice uses **Upload Sync** to pull Bob's entire upload library into her account. The ML data (CLIP embeddings, face detection, face recognition) is copied along with the assets, so Alice gets full search and face recognition on Bob's photos without any duplicate GPU processing.

Both sync methods run together in the same sidecar, syncing into Alice's and Bob's accounts respectively.

## Setup

### Quick Setup (Recommended)

The interactive setup wizard handles everything: connecting to Immich, detecting paths, creating symlinks and libraries, and generating the `.env` file.

**Prerequisites:** Create an admin API key in Immich (**Account Settings > API Keys**) and have `python3` installed on the host.

```bash
python3 setup.py
```

The wizard will:

1. Connect to your Immich server and verify admin access
2. Auto-detect volume mount paths from Immich's `docker-compose.yml`
3. Let you choose which sync method(s) to configure (external library, upload sync, or both)
4. Walk you through selecting source/target users
5. Create the necessary symlinks and external libraries in Immich (with `**/*` exclusion so Immich won't scan them)
6. Optionally set up album assignment
7. Generate a `.env` file with all the correct UUIDs and path prefixes

Once the wizard completes, start the sidecar:

```bash
docker compose up -d
```

If you already have a `.env` file, re-running the wizard will use your existing values as defaults and let you add or reconfigure sync methods.

### Manual Setup

If the wizard doesn't suit your environment, or you need to understand what each setting does (useful for troubleshooting), follow the manual steps below.

#### 1. Create an API key

In Immich, go to **Account Settings > API Keys** and create a key.

#### 2. Configure environment

```bash
cp env.example .env
```

Edit `.env` with the common settings:

```env
DB_PASSWORD=postgres

# Host paths to Immich's data directories
UPLOAD_LOCATION=../immich-app/library
EXTERNAL_LIBRARY_DIR=../immich-app/external_library

IMMICH_API_KEY=your-immich-api-key
TARGET_USER_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
```

#### 3. Find UUIDs

**User IDs:** In the Immich web UI, go to **Administration > Users** and click on a username. The UUID appears in the URL: `/admin/users/{UUID}`.

**Library IDs:** In the Immich web UI, go to **Administration > External Libraries** and click on a library. The UUID appears in the URL: `/admin/library-management/{UUID}`.

Now follow the instructions for the sync method(s) you want to use, then skip to [Album Assignment](#optional-album-assignment) and [Start the Sidecar](#start-the-sidecar).

---

## External Library Sync

Syncs assets from a source user's external library into the target user's account. Use this when both users should see the same set of externally-managed photos (e.g., a shared photo directory on a NAS).

### How it works

The source user (User A) has an external library that Immich scans and processes normally. You create a *second* external library for the target user (User B) containing a symlink to the same photos. The sidecar creates asset records for User B directly in the database — Immich never scans this target library.

### Step 1: Create the symlink

Create a symlink so the target user's external library points to the source user's photos:

```
/external_library/
  user_a/               # Source user's external library (scanned by Immich)
    photos/
      photo1.jpg
      photo2.jpg
  user_b_shared/        # Target user's external library (NOT scanned — sidecar handles it)
    photos -> ../user_a/photos
```

You can symlink the entire source directory or just a subdirectory — the `SHARED_PATH_PREFIX` controls which source assets are synced.

### Step 2: Create the target external library in Immich

Create a new external library for the target user (User B) in Immich with an import path that covers the symlink directory (e.g., `/external_library/user_b_shared/`).

**Important:** This library must not be scanned by Immich. Add the exclusion pattern `**/*` to tell Immich to ignore all files:

1. Go to **Administration > External Libraries**
2. Click on the target user's new external library
3. Add `**/*` as an exclusion pattern and save

The sidecar writes asset records directly to the database. Immich sees User B's assets because they exist in the `asset` table, not because it scanned the filesystem.

### Step 3: Enable library watching

In Immich's **Administration > Settings > External Library**, enable **Library Watching**. This uses filesystem events (inotify) to detect new files added to the *source* user's external library. Immich will process them through the ML pipeline. Once processing completes, the sidecar picks them up on the next sync cycle.

Because the target library has the `**/*` exclusion pattern, Immich will ignore file events in that library.

> **Note:** The sidecar does not trigger library scans itself. It relies on Immich's library watching (or manual scans) to discover new source files. If library watching doesn't work in your environment (e.g., network drives), you can trigger scans manually:
> ```bash
> curl -X POST "http://localhost:2283/api/libraries/SOURCE_LIBRARY_ID/scan" \
>   -H "x-api-key: YOUR_KEY"
> ```

### Step 4: Add to `.env`

```env
SOURCE_USER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TARGET_LIBRARY_ID=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz

# Path prefixes as seen inside the Immich container (not on the host)
SHARED_PATH_PREFIX=/external_library/user_a/photos/
TARGET_PATH_PREFIX=/external_library/user_b_shared/photos/
```

---

## Upload Sync

Syncs app/website uploads from a source user into the target user's account. Use this when the source user uploads photos via the Immich mobile app or web UI and you want them to appear in the target user's library too.

### How it works

The source user (User C) uploads photos normally — Immich stores them in its upload directory and runs ML processing. You create an external library for the target user (User B) containing a symlink to User C's upload directory. The sidecar creates asset records for User B directly in the database.

### Step 1: Create the symlink

Create a symlink from a directory inside the target user's external library to the source user's upload directory:

```
/external_library/
  user_b_uploads/     # Target user's external library (NOT scanned — sidecar handles it)
    user_c -> /data/upload/cccccccc-cccc-cccc-cccc-cccccccccccc/
```

The symlink target must point to the source user's upload directory *inside the container*. Immich stores uploads at `{upload_location}/upload/{user_id}/`.

### Step 2: Create the target external library in Immich

Create a **new** external library for the target user (User B) in Immich. This must be a **separate library** from any library used for external library sync.

**Important:** This library must not be scanned by Immich. Add the exclusion pattern `**/*`:

1. Go to **Administration > External Libraries**
2. Click on the target user's new upload sync library
3. Add `**/*` as an exclusion pattern and save

> **Why a separate library?** The source user's libraries are actively scanned by Immich to discover and process new assets. The upload sync target library must *not* be scanned (Immich would create duplicate records and run redundant ML processing). The sidecar handles creating the asset records directly in the database.

### Step 3: Add to `.env`

```env
# Source user whose uploads you want to sync
UPLOAD_SOURCE_USER_ID=cccccccc-cccc-cccc-cccc-cccccccccccc

# The new external library created above (separate from any external library sync library)
UPLOAD_TARGET_LIBRARY_ID=wwwwwwww-wwww-wwww-wwww-wwwwwwwwwwww

# Path prefix where the symlink maps uploads into the external library
TARGET_UPLOAD_PATH_PREFIX=/external_library/user_b_uploads/user_c/
```

The sidecar automatically derives the source path prefix from the upload location mount and the source user ID (e.g., `/usr/src/app/upload/upload/cccccccc-cccc-cccc-cccc-cccccccccccc/`).

> **Note:** If you're only doing upload sync, you don't need `SOURCE_USER_ID`, `TARGET_LIBRARY_ID`, `SHARED_PATH_PREFIX`, or `TARGET_PATH_PREFIX`. Those are only for external library sync.

---

## Optional: Album Assignment

To have all synced assets (from both sync methods) automatically added to an album in the target user's account, create the album first in Immich, then add its UUID to `.env`:

```env
TARGET_ALBUM_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
```

The album must be owned by the target user.

If you add `TARGET_ALBUM_ID` after assets have already been synced, the sidecar will backfill all previously synced assets into the album on the next cycle. When a source asset is deleted, its album entry is also removed during cleanup.

## Start the Sidecar

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
| `TARGET_USER_ID` | *(required)* | UUID of the target user |
| **External library sync** | | *Required if `SHARED_PATH_PREFIX` is set* |
| `SOURCE_USER_ID` | | UUID of the source user (external library) |
| `TARGET_LIBRARY_ID` | | UUID of the target user's external library |
| `SHARED_PATH_PREFIX` | | Path prefix for source assets inside the container (e.g., `/external_library/user_a/shared/`) |
| `TARGET_PATH_PREFIX` | | Path prefix for target assets inside the container (e.g., `/external_library/user_b/shared/`) |
| **Upload sync** | | *Required if `UPLOAD_SOURCE_USER_ID` is set* |
| `UPLOAD_SOURCE_USER_ID` | | UUID of the source user whose uploads to sync |
| `UPLOAD_TARGET_LIBRARY_ID` | | UUID of a separate external library for the target user (with `**/*` exclusion) |
| `TARGET_UPLOAD_PATH_PREFIX` | | Path prefix where the upload symlink maps into the external library |
| **Album** | | |
| `TARGET_ALBUM_ID` | | UUID of album to add all synced assets to (must be owned by target user) |
| **General** | | |
| `SYNC_INTERVAL_SECONDS` | `60` | Seconds between sync cycles |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

At least one of `SHARED_PATH_PREFIX` or `UPLOAD_SOURCE_USER_ID` must be set. Both can be configured simultaneously to sync from multiple sources.

## How the Sync Works

Each sync cycle runs five phases:

1. **New assets** — For each configured sync job (external library, uploads), finds fully-processed source assets not yet synced. Creates target asset records with copied EXIF, CLIP embeddings, faces, and hardlinked thumbnails.
1b. **Album assignment** — Adds newly synced assets to the target album (if configured). Backfills any previously synced assets that are missing from the album.
2. **Incremental faces** — Detects face updates on already-synced assets (using a watermark timestamp) and copies new faces.
3. **Person metadata** — Syncs person name changes, visibility (`isHidden`), and thumbnail updates from source to target.
4. **Cleanup** — Removes target assets (and their album entries) whose source was deleted or trashed. Detects person merges (face reassignment). Removes orphaned target persons.

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

### Testing

1. Run the setup wizard to configure your `.env` and connect to a local Immich instance:

   ```bash
   python3 setup.py
   ```

2. Copy some photos into the source user's watched folders or upload them via the Immich app/web UI. Wait for Immich to finish processing (metadata, thumbnails, CLIP, faces).

3. Run a sync cycle to create the target assets:

   ```bash
   ./run-utility.sh test_sync.py
   ```

4. Verify `dedup_synced.py` detects duplicates (if the target user also has copies of the same photos):

   ```bash
   ./run-utility.sh dedup_synced.py
   ```

5. Run `delete_synced.py` and confirm it shows the correct number of synced assets:

   ```bash
   ./run-utility.sh delete_synced.py
   ```

6. Run `test_sync.py` again — it will recreate the deleted assets, confirming the full round-trip works.

### Utility Scripts

The utility scripts read configuration from `.env` (the same file used by `docker compose`). Since the Immich PostgreSQL container doesn't expose port 5432 by default, they must run inside a Docker container on the Immich network. `run-utility.sh` handles the Docker invocation:

```bash
./run-utility.sh test_sync.py
./run-utility.sh dedup_synced.py --match-time
./run-utility.sh delete_synced.py
```

- **`test_sync.py`** — Run a single sync cycle and print verification queries.
- **`delete_synced.py`** — Delete all synced assets for a target user. Does not mark sources as skipped, so running the sync engine again will recreate everything. Useful for resetting a target account.
- **`dedup_synced.py`** — Detect and remove synced assets that duplicate the target user's own uploads (matched by filename + capture date). Use `--match-time` to compare the full timestamp (with TZ normalisation) instead of just the date. Marks duplicates as skipped so the sync engine won't recreate them.

`delete_synced.py` and `dedup_synced.py` are interactive: they show a summary and prompt for confirmation before making changes, with a dry-run option.

### Project Structure

```
src/
  main.py          — Entry point: config validation, health check, concurrent loops
  sync_engine.py   — Orchestrates 5-phase sync cycle
  asset_sync.py    — Asset record creation, EXIF copy, path remapping
  ml_sync.py       — Face and embedding sync
  person_sync.py   — Person mirroring, name/visibility sync
  album_sync.py    — Album assignment and backfill
  cleanup.py       — Deletion detection and cleanup
  file_ops.py      — Hardlink creation and removal
  db.py            — asyncpg connection pool and transaction helpers
  config.py        — SyncJob dataclass, Pydantic Settings for environment variables
  immich_api.py    — Immich REST API client (health check)
  health.py        — TCP health check server
```

### Key Things to Know

- Immich tables are **singular** (`asset`, not `assets`) with **camelCase** columns that must be double-quoted in SQL.
- The sidecar creates two tracking tables prefixed with `_face_sync_` to avoid colliding with Immich's schema.
- Each asset sync uses a PostgreSQL SAVEPOINT so one failure doesn't roll back the entire batch.
- Cleanup deletes hardlinked files before DB records to avoid orphan files on crash.
