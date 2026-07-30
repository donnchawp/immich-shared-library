# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Teaching Mode

The user is learning Python through this project. When making code changes:

- Briefly explain **what** the code does and **why**, focusing on Python concepts that may be unfamiliar (e.g., async/await, context managers, list comprehensions, type hints, decorators)
- After making changes, ask if anything needs further explanation
- Keep explanations concise — a sentence or two per concept, not a lecture
- Use the actual code being changed as the teaching example, not abstract examples

## What This Is

A Python sidecar service that syncs a subset of Immich photos from a source user to a target user by directly creating pre-populated asset records with copied ML data (CLIP embeddings, face detection, face recognition) and hardlinked thumbnails. This eliminates duplicate ML processing when sharing external libraries between users.

## Commands

**Install dependencies locally:**
```bash
pip install -e .
```

**Run the service (Docker, production mode):**
```bash
docker compose up --build
```

**Run a single manual sync cycle (for testing):**
```bash
# Must run inside a Docker container on the immich_default network
# (Postgres is not exposed to the host)
docker run --rm --network immich_default \
  -v $(pwd):/app \
  -v /path/to/immich-app/library:/data \
  -v /path/to/immich-app/external_library:/external_library \
  -e DB_HOSTNAME=<postgres-container-ip> \
  -e DB_PORT=5432 -e DB_USERNAME=postgres -e DB_PASSWORD=postgres \
  -e DB_DATABASE_NAME=immich \
  -e IMMICH_API_URL=http://immich_server:2283 \
  -e IMMICH_API_KEY=<key> \
  -e SOURCE_USER_ID=<uuid> -e TARGET_USER_ID=<uuid> \
  -e TARGET_LIBRARY_ID=<uuid> \
  -e SHARED_PATH_PREFIX=/external_library/source_user/ \
  -e TARGET_PATH_PREFIX=/external_library/target_user/ \
  -e LOG_LEVEL=DEBUG \
  -w /app python:3.12-slim \
  bash -c 'pip install asyncpg httpx pydantic pydantic-settings && python test_sync.py'
```

There are no automated tests yet. `test_sync.py` is a manual integration test that runs one full sync cycle and prints verification queries.

## Architecture

### Sync Engine (4 phases, each in its own transaction)

1. **New asset sync** (`asset_sync.py`): Finds source assets with completed ML processing not yet in `_face_sync_asset_map`. For each, creates a target asset record with remapped paths, copies EXIF, hardlinks thumbnails, copies CLIP embeddings, copies faces + face embeddings, creates mirrored persons. Uses SAVEPOINTs so one asset failure doesn't abort the batch.

2. **Incremental face sync** (`ml_sync.py`): Watermark-based — only checks assets where `asset_face.updatedAt > _face_sync_asset_map.synced_at`.

3. **Person metadata sync** (`person_sync.py`): Syncs name changes, `isHidden` visibility, and thumbnail paths from source to target persons.

4. **Cleanup** (`cleanup.py`, `person_sync.py`): Prunes mappings whose target asset was hard-deleted in Immich (so the source re-syncs next cycle; trashed targets keep their mapping), removes target assets whose source was deleted/trashed, handles person merges (bounding-box matching to detect face reassignment), removes orphaned target persons.

### Key modules

- `sync_engine.py` — Orchestrates the 4 phases, returns stats dict
- `asset_sync.py` — Asset record creation with savepoint rollback, idempotency check, path remapping
- `ml_sync.py` — Face record copying with bounding-box dedup, face_search embedding copy
- `person_sync.py` — Person mirroring, thumbnail hardlinking, name/visibility sync, orphan cleanup
- `cleanup.py` — Deletion detection (LEFT JOIN on source), hardlink removal before DB deletion
- `file_ops.py` — Hardlink creation/removal, path remapping by exact UUID component matching
- `db.py` — asyncpg pool (min=2, max=10), `transaction()` context manager, query helpers
- `config.py` — SyncJob dataclass, YAML config loader, Pydantic Settings (env var fallback)
- `main.py` — Entry point: config validation, health wait, DB init, concurrent sync + scan loops
- `immich_api.py` — httpx AsyncClient (single instance) for Immich REST API (health check)
- `health.py` — TCP health check server on port 8080

### Configuration

Per-job config can come from either `config.yaml` (preferred for multi-job setups) or env vars (legacy, backward compat). If `config.yaml` exists at `/app/config.yaml` (or `CONFIG_FILE` env var path), it takes priority. Album assignment is per-job (`album_id` in YAML, or global `TARGET_ALBUM_ID` for env var mode).

### Tracking tables (created automatically)

- `_face_sync_asset_map` — Maps `source_asset_id` <-> `target_asset_id` with `synced_at` watermark
- `_face_sync_person_map` — Maps `source_person_id` <-> `target_person_id` per `target_user_id`

## Immich Schema Constraints

All Immich table names are **singular** (`asset`, `asset_face`, `person`, etc.). Column names are **camelCase** and must be double-quoted in SQL (`"ownerId"`, `"originalPath"`, `"deletedAt"`).

Key relationships:
- `person.faceAssetId` is FK to `asset_face.id` (not `asset.id`)
- `face_search.embedding` and `smart_search.embedding` are `vector(512)` (pgvector)
- Asset checksum unique constraint: `(ownerId, libraryId, checksum) WHERE libraryId IS NOT NULL`
- Album ownership (since Immich v3): `album_user` row with `role = 'owner'` — `album."ownerId"` no longer exists
- `asset.duration` (since Immich v3): integer milliseconds (was varchar)
- OCR results (since Immich v3): `asset_ocr` (text boxes) + `ocr_search` (search text), both CASCADE from `asset`
- Video stream metadata (since Immich v3): `asset_video`/`asset_audio`/`asset_keyframe`, all PK on `assetId`, CASCADE from `asset`; videos also have an `encoded_video` row in `asset_file`
- `asset.isEdited` is **derived, not authoritative**: statement-level triggers on `asset_edit` own it (`asset_edit_insert` sets it true, `asset_edit_delete` sets it false when the last edit row goes). Never INSERT it directly — copy the `asset_edit` rows and let the trigger flip it, or the flag desynchronizes from the rows it summarizes. Both delete triggers are guarded by `pg_trigger_depth() = 0`, so a CASCADE delete from `asset` skips them.
- `asset_edit` (since Immich v3): `(assetId, action, parameters, sequence)`, unique on `(assetId, sequence)`, CASCADE from `asset`. `parameters` is pure geometry (crop x/y/w/h, rotate angle, mirror axis) — no ids or paths, so it copies verbatim.
- Thumbnail path: `/data/thumbs/{userId}/{assetId[0:2]}/{assetId[2:4]}/{assetId}_{type}.{ext}`
- Person thumbnail: `/data/thumbs/{userId}/{personId[0:2]}/{personId[2:4]}/{personId}.jpeg`

### Why pre-populating works (Immich skip logic)

Immich skips ML processing for assets that already have records:
- Library scan skips paths with existing asset records
- CLIP encoding skips assets with existing `smart_search` record
- Face detection skips when `asset_job_status.facesRecognizedAt IS NOT NULL`
- Face recognition skips faces with `personId` already assigned
- **WARNING**: `force=true` on any Immich job bypasses all skip logic

## Design Decisions

- **Hardlinks** (not copies) for thumbnails — zero extra disk space, requires same filesystem mount
- **Savepoints** per asset — single failure doesn't abort the batch transaction
- **Watermark-based incremental sync** — avoids O(n) full-table scan for face updates
- **Files deleted before DB records** during cleanup — prevents orphan files on crash
- **Idempotency check** before asset creation — looks for existing (ownerId, libraryId, originalPath)
- **Bounding-box matching** for face merge detection — exact coordinate comparison across source/target
