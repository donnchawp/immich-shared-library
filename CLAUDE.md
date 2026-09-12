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

The repo has an automated test suite (pytest) run against a scratch `immich_test` database built from a real schema dump. `make help` lists the targets: `test`, `testdb`, `testdb-clean`, `schema-dump`, `lint`. Postgres isn't published to the host, so `make test` runs pytest in a container on the `immich_default` network — running `pytest` directly on the host will fail to connect. `test_sync.py` remains the manual integration test that runs one full sync cycle against a real, live Immich instance and prints verification queries; its invocation is unchanged, only its printed output labels changed for v3.2.0.

## Architecture

### Sync Engine (5 phases, each in its own transaction)

1. **New asset sync** (`asset_sync.py`): Finds source assets with completed ML processing not yet in `_face_sync_asset_map`. For each, creates a target asset record with remapped paths, copies EXIF, hardlinks thumbnails, copies CLIP embeddings, copies faces (`ml_sync.py`, which copies `asset_face."personGroupId"` verbatim, ensures the target has a `person` row for that group, and deliberately does *not* copy the face embedding). Uses SAVEPOINTs so one asset failure doesn't abort the batch.

1b. **Album assignment** (`album_sync.py`): Adds newly synced assets to the target album, backfills previously synced assets missing from it.

2. **Incremental face sync** (`ml_sync.py`): Watermark-based — only checks assets where `asset_face.updatedAt > _face_sync_asset_map.synced_at`.

3. **Person metadata sync** (`person_sync.py`): `sync_person_names` only fills an *empty* target name — it never overwrites a name the target user set (names are per-user by design in v3.2.0). `sync_person_thumbnails` hardlinks thumbnails that are still empty. **Visibility is deliberately not synced**: `isHidden` is a plain boolean with no "unset" sentinel, so there is no fill-only option, and each user owns their own. The target inherits the source's `isHidden` once, at person creation. `tests/test_person_sync.py::test_no_cycle_function_overwrites_the_target_visibility` discovers and runs every public `async def f(conn)` in `person_sync` to guard this.

4. **Cleanup** (`cleanup.py`, `person_sync.py`): Prunes mappings whose target asset was hard-deleted in Immich (so the source re-syncs next cycle; trashed targets keep their mapping), removes target assets whose source was deleted/trashed, reassigns target faces back to the source's `personGroupId` when they've drifted (`cleanup_reassigned_faces` — the source is authoritative, so a target-side reassignment is reverted next cycle), removes orphaned target persons.

### Key modules

- `sync_engine.py` — Orchestrates the 5 phases, returns stats dict
- `asset_sync.py` — Asset record creation with savepoint rollback, idempotency check, path remapping
- `ml_sync.py` — Face record copying with bounding-box dedup, `personGroupId` copied verbatim. Copies carry **no `face_search` row** and `sourceType = 'manual'`, which together keep them out of Immich's facial recognition (see "Why copied faces are invisible to recognition")
- `person_sync.py` — Target person row creation (`ensure_target_person`), thumbnail hardlinking, name sync, orphan cleanup (~300 lines; shrank from 422 when person mirroring was removed)
- `cleanup.py` — Deletion detection (LEFT JOIN on source), hardlink removal before DB deletion, source-authoritative face reassignment
- `file_ops.py` — Hardlink creation/removal, path remapping by exact UUID component matching
- `db.py` — asyncpg pool (min=2, max=10), `transaction()` context manager, query helpers
- `config.py` — SyncJob dataclass, YAML config loader, Pydantic Settings (env var fallback)
- `main.py` — Entry point: config validation, tracking-table migrations, health wait, DB init, concurrent sync + scan loops
- `schema.py` — Schema validation plus `validate_cluster_group()`, which refuses to start unless every configured source and target user shares one `clusterGroupId` (soft-deleted users count as missing)
- `immich_api.py` — httpx AsyncClient (single instance) for Immich REST API (health check)
- `health.py` — TCP health check server on port 8080

### Configuration

Per-job config can come from either `config.yaml` (preferred for multi-job setups) or env vars (legacy, backward compat). If `config.yaml` exists at `/app/config.yaml` (or `CONFIG_FILE` env var path), it takes priority. Album assignment is per-job (`album_id` in YAML, or global `TARGET_ALBUM_ID` for env var mode).

### Tracking tables (created automatically)

- `_face_sync_asset_map` — Maps `source_asset_id` <-> `target_asset_id` with `synced_at` watermark. Indexed on `(target_user_id, source_user_id)` (`_face_sync_asset_map_user_pair_idx`) to support the person queries in `person_sync.py`.
- `_face_sync_meta` — Sidecar's own schema version (`SCHEMA_VERSION = 4`). `_migrate_v3()` drops the retired `_face_sync_person_map` table on upgrade — person identity moved to Immich's `person_group`, so there's nothing left to map. `_migrate_v4()` deletes the `face_search` rows of already-copied faces and flips their `sourceType` to `'manual'`, retiring the distance-0 twins earlier versions created.
- `_face_sync_skipped` — Source assets deliberately skipped (e.g. detected duplicates), keyed by `(source_asset_id, target_user_id)`.

## Immich Schema Constraints

All Immich table names are **singular** (`asset`, `asset_face`, `person`, etc.). Column names are **camelCase** and must be double-quoted in SQL (`"ownerId"`, `"originalPath"`, `"deletedAt"`).

Key relationships:
- Since Immich v3.2.0: person identity lives in `person_group` (FK to `cluster_group`). `person.id` is **gone** — PK is `("ownerId", "personGroupId")`. `asset_face."personId"` is now `"personGroupId"`, FK to `person_group`, `ON DELETE SET NULL`.
- `person.faceAssetId` is still an FK to `asset_face.id` — it must point at a face the *same user* owns.
- `face_search.embedding` and `smart_search.embedding` are `vector(512)` (pgvector)
- Asset checksum unique constraint: `(ownerId, libraryId, checksum) WHERE libraryId IS NOT NULL`
- Album ownership (since Immich v3): `album_user` row with `role = 'owner'` — `album."ownerId"` no longer exists
- `asset.duration` (since Immich v3): integer milliseconds (was varchar)
- OCR results (since Immich v3): `asset_ocr` (text boxes) + `ocr_search` (search text), both CASCADE from `asset`
- Video stream metadata (since Immich v3): `asset_video`/`asset_audio`/`asset_keyframe`, all PK on `assetId`, CASCADE from `asset`; videos also have an `encoded_video` row in `asset_file`
- `asset.isEdited` is **derived, not authoritative**: statement-level triggers on `asset_edit` own it (`asset_edit_insert` sets it true, `asset_edit_delete` sets it false when the last edit row goes). Never INSERT it directly — copy the `asset_edit` rows and let the trigger flip it, or the flag desynchronizes from the rows it summarizes. Both delete triggers are guarded by `pg_trigger_depth() = 0`, so a CASCADE delete from `asset` skips them.
- `asset_edit` (since Immich v3): `(assetId, action, parameters, sequence)`, unique on `(assetId, sequence)`, CASCADE from `asset`. `parameters` is pure geometry (crop x/y/w/h, rotate angle, mirror axis) — no ids or paths, so it copies verbatim.
- Thumbnail path: `/data/thumbs/{userId}/{assetId[0:2]}/{assetId[2:4]}/{assetId}_{type}.{ext}`
- Person thumbnail (since Immich v3.2.0, keyed on the person **group**, not the person): `/data/thumbs/{ownerId}/{personGroupId[0:2]}/{personGroupId[2:4]}/{personGroupId}.jpeg`
- Immich's `deleteEmptyGroups` drops any `person_group` with no `person` row and nulls its faces — always create the target's `person` row (`ensure_target_person`) rather than leaving a bare `personGroupId` reference.
- `user.clusterGroupId` is a single `NOT NULL uuid`: a user is in exactly one cluster group. `validate_cluster_group()` checks this for every configured user and refuses to start on a mismatch.

### Why copied faces are invisible to recognition

A copied face gets **no `face_search` row** and `sourceType = 'manual'`. Both are required, and for different reasons:

- **No embedding.** `searchFaces()` inner-joins `face_search`, so a face without one is never a recognition candidate. A copied embedding is byte-identical to its source — a distance-0 twin — and `handleRecognizeFaces` counts matches against `minFaces` to decide whether a cluster is "core". Copying it made every shared face vote twice, so a person appearing in two synced photos cleared a threshold of 3 and became a person who should not exist.
- **Non-ML `sourceType`.** `getAllFaces()` only queues `machine-learning` faces, and `handleRecognizeFaces` rejects anything else *before* it checks for an embedding. Without this, a cluster-wide reset would queue every copy and fail each one with "does not have an embedding".

`'manual'` and not `'exif'`: metadata extraction deletes every exif-sourced face on an asset and rebuilds it from XMP regions (`metadata.service.ts`), and the sidecar syncs XMP sidecars. Nothing in Immich deletes or rewrites manual faces — `deleteFaces` and `unassignFaces` are both scoped to `machine-learning`.

The cost: the target's faces can no longer be recognized independently, which is the design (the source is authoritative for identity) but means leaving the cluster group requires a face-detection re-run. A consequence to know: a force face-detection run deletes the source's ML faces but leaves the manual copies, where previously both went. Phase 2's bounding-box `NOT EXISTS` keeps the existing copy and `cleanup_reassigned_faces` refreshes its group, so it self-heals — unless re-detection shifts a bounding box, which strands the old copy alongside the new one.

### Why pre-populating works (Immich skip logic)

Immich skips ML processing for assets that already have records:
- Library scan skips paths with existing asset records
- CLIP encoding skips assets with existing `smart_search` record
- Face detection skips when `asset_job_status.facesRecognizedAt IS NOT NULL`
- Face recognition skips faces with `personGroupId` already assigned
- **WARNING**: `force=true` on any Immich job bypasses all skip logic

## Design Decisions

- **Hardlinks** (not copies) for thumbnails — zero extra disk space, requires same filesystem mount
- **Savepoints** per asset — single failure doesn't abort the batch transaction
- **Watermark-based incremental sync** — avoids O(n) full-table scan for face updates
- **Files deleted before DB records** during cleanup — prevents orphan files on crash
- **Idempotency check** before asset creation — looks for existing (ownerId, libraryId, originalPath)
- **Bounding-box matching** for face reassignment — exact coordinate comparison detects when a target face's `personGroupId` has drifted from the source's, so it can be reverted (source is authoritative; no merge/mirror mapping to maintain since person identity is shared, not mirrored)
