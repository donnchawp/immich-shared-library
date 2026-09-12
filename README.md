# Immich Shared Library

A Docker sidecar service that syncs a subset of [Immich](https://immich.app/) photos from one user to another, without duplicating ML processing.

## The Problem

Immich's built-in partner sharing lets you view another user's library, but it shares your entire library or nothing, and shared assets stay in the other user's account, so they don't land in your own timeline, memories, or albums. Since v3.2.0 cluster groups do let you see your own people in shared assets, so if a shared album is enough for you, you may not need this sidecar at all. Use it when you want the other user to genuinely *own* a curated subset of the photos.

The common workaround is symlinking an external library directory so both users point at the same photos. This gives each user their own copy of the assets with full face recognition support, but Immich processes each user's assets independently through the full ML pipeline:

| Per-asset work | Symlink only | With sidecar |
|---|---|---|
| Metadata extraction (EXIF) | 2x | 1x (copied) |
| Thumbnail generation | 2x | 1x (hardlinked) |
| CLIP embedding (smart search) | 2x (GPU) | 1x (copied) |
| Face detection | 2x (GPU) | 1x (copied) |
| Face recognition | 2x (GPU) | 1x (copied) |
| Person clustering | Independent per user | Shared identity (`personGroupId` copied verbatim) |
| Person names | Must name separately | Auto-filled once if empty; never overwritten after |

With 1,000 shared photos, the symlink approach queues 5,000+ extra ML jobs (metadata, thumbnails, CLIP, face detection, face recognition) that produce identical results. Each user also gets independent person clusters, so you'd need to name each person twice, and the clusters may group faces differently.

## How It Works

This sidecar connects directly to Immich's PostgreSQL database and, for each shared source asset:

1. Creates a target asset record with remapped file paths
2. Copies EXIF metadata, CLIP embeddings, face detection results, face recognition data, and edit history (crop/rotate/mirror)
3. Hardlinks thumbnail and preview files (zero extra disk space)
4. Copies each face's `personGroupId` verbatim and, if the target has no `person` row for that group yet, creates one (with hardlinked face thumbnail). Person identity is shared across the cluster group, so there's nothing to mirror
5. Pre-populates job status so Immich skips all ML processing for these assets

The target user's assets appear instantly with full search, face recognition, and timeline support. Immich queues no ML jobs for them and spends no GPU time on them. The sidecar runs continuously, syncing new assets, filling in empty person names, and cleaning up deletions.

## Prerequisites

- **Docker** with `docker compose`. The sidecar builds and runs entirely in Docker, so you need nothing else installed
- **Immich v3.2.0** (tested). Earlier versions are not supported: v3.2.0 moved person identity into `person_group` and dropped `person.id`. Other v3.2.x versions may work but the database schema can change between releases, so check the [Immich release notes](https://github.com/immich-app/immich/releases) before upgrading. Note: since v3, album ownership lives in `album_user` (role `owner`), and the sidecar copies OCR results (`asset_ocr`/`ocr_search`) and video stream metadata (`asset_video`/`asset_audio`/`asset_keyframe`) alongside CLIP embeddings.
- **All configured users must share one Immich cluster group** (Account Settings > Sharing > Cluster group). The sidecar copies `asset_face."personGroupId"` verbatim, so source and target must resolve the same identities. It refuses to start otherwise. Joining a group does *not* delete anyone: Immich moves your existing person groups across intact. But until you run **Reset facial recognition** for the group, every member keeps their own separate person groups, so the same human stays split across members and the sidecar has no shared identity to copy. That reset is the step that costs you something: it deletes all people for all users in the group, and existing names and birth dates are lost. Plan for it before you configure anything else. If you are upgrading an instance that already ran an older version of this sidecar, read [Upgrading from Immich v3.1.x](#upgrading-from-immich-v31x) first.
- Two or more Immich users (at least one source and one target)
- Source assets must be fully processed by Immich (metadata, faces, CLIP)

> **Back up your database before running this sidecar.** It writes directly to Immich's PostgreSQL database. If something goes wrong, you'll want a backup to restore from. A simple `pg_dump` is enough:
> ```bash
> docker exec immich_postgres pg_dump -U postgres immich > immich_backup.sql
> ```

The sidecar supports two sync methods. You can use one or both:

| Method | What it syncs | Source |
|---|---|---|
| **External Library Sync** | A subset (or all) of one user's external library | Source user's external library directory |
| **Upload Sync** | App/website uploads from a different user | Source user's upload directory (camera roll, web uploads, etc.) |

### Example scenario

Alice manages the family photo library in Lightroom Classic and imports the finished edits into Immich via an external library. She uses **External Library Sync** to share a curated subset of family photos with Bob: just the family shots, not the street photography or landscapes.

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
5. Create the necessary symlinks and external libraries in Immich (with `**/*` exclusion so Immich won't scan them; you must also disable Immich's periodic scan, see [Setup](#setup))
6. Optionally set up album assignment
7. Generate a `.env` file with all the correct UUIDs and path prefixes

Once the wizard completes, start the sidecar:

```bash
docker compose up -d
```

If you already have a `.env` file, re-running the wizard will use your existing values as defaults and let you add or reconfigure sync methods.

### Multi-Job Configuration (config.yaml)

For setups with more than one sync job, or for cleaner configuration, create a `config.yaml` file. This separates per-job settings from infrastructure settings (which stay in `.env`).

```yaml
# config.yaml
sync_jobs:
  - name: "alice-external-to-bob"
    source_user_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    target_user_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    target_library_id: "llllllll-llll-llll-llll-llllllllllll"
    source_path_prefix: "/external_library/alice/photos/"
    target_path_prefix: "/external_library/bob_shared/photos/"
    album_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  # optional, per-job

  - name: "charlie-uploads-to-alice"
    source_user_id: "cccccccc-cccc-cccc-cccc-cccccccccccc"
    target_user_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    target_library_id: "mmmmmmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm"
    source_path_prefix: "/usr/src/app/upload/library/cccccccc-cccc-cccc-cccc-cccccccccccc/"
    target_path_prefix: "/external_library/alice_uploads/charlie/"
```

```env
# .env (infrastructure only)
DB_PASSWORD=postgres
UPLOAD_LOCATION=../immich-app/library
EXTERNAL_LIBRARY_DIR=../immich-app/external_library
IMMICH_API_KEY=your-key
```

Mount the config file in `docker-compose.yml` by uncommenting the volume line:

```yaml
volumes:
  - ./config.yaml:/app/config.yaml:ro
```

The setup wizard (`python3 setup.py`) generates both files and enables the volume mount automatically.

**Backward compatibility:** If no `config.yaml` exists, the sidecar falls back to per-job environment variables (`SOURCE_USER_ID`, `TARGET_USER_ID`, etc.), so existing `.env`-only deployments continue to work unchanged.

> **Migrating from env vars to config.yaml:** Create a `config.yaml` with your job(s), move album config from `TARGET_ALBUM_ID` to per-job `album_id`, add the volume mount to `docker-compose.yml`, and remove the per-job env vars from `.env`. Re-running `python3 setup.py` does this automatically.

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

Now configure one or both sync methods ([external library](#external-library-sync) or [upload](#upload-sync)), then continue to [Album Assignment](#4-album-assignment-optional) and [Start the Sidecar](#5-start-the-sidecar).

#### External Library Sync

Syncs assets from a source user's external library into the target user's account. Use this when both users should see the same set of externally-managed photos (e.g., a shared photo directory on a NAS).

The source user (User A) has an external library that Immich scans and processes normally. You create a *second* external library for the target user (User B) containing a symlink to the same photos. The sidecar creates asset records for User B directly in the database. Immich never scans this target library.

**Create the symlink** so the target user's external library points to the source user's photos:

```
/external_library/
  user_a/               # Source user's external library (scanned by Immich)
    personal/
      photo1.jpg
    shared/             # Subdirectory to share with User B
      photo2.jpg
      photo3.jpg
  user_b_shared -> user_a/shared   # Target user's external library (NOT scanned, sidecar handles it)
```

You can symlink the entire source directory or just a subdirectory. The `SHARED_PATH_PREFIX` controls which source assets are synced.

**Create the target external library in Immich** for the target user (User B) with an import path that covers the symlink directory (e.g., `/external_library/user_b_shared/`).

**Important:** This library must not be scanned by Immich. Add the exclusion pattern `**/*` to tell Immich to ignore all files:

1. Go to **Administration > External Libraries**
2. Click on the target user's new external library
3. Add `**/*` as an exclusion pattern and save

The sidecar writes asset records directly to the database. Immich sees User B's assets because they exist in the `asset` table, not because it scanned the filesystem.

**Enable library watching** in Immich's **Administration > Settings > External Library**. This uses filesystem events (inotify) to detect new files added to the *source* user's external library. Immich will process them through the ML pipeline. Once processing completes, the sidecar picks them up on the next sync cycle.

Because the target library has the `**/*` exclusion pattern, Immich will ignore file events in that library.

> **⚠️ Disable Immich's periodic scan, and never run a manual scan on a target library.**
>
> Library *watching* (inotify) respects the `**/*` exclusion and is safe. A *scan* is not: it crawls the target library, finds 0 files (because of the exclusion), concludes every sidecar-managed asset is missing from disk, and marks them all **offline**, which soft-deletes them so the target user's shared photos disappear. A full library's worth of assets can be offlined in a single scan.
>
> To avoid this:
> 1. Turn off **Administration > Settings > External Library > Periodic Scanning** (or set no cron schedule). Rely on library watching to discover new *source* files instead.
> 2. Never click **Scan** on a target library in the UI.
>
> Do **not** work around this by removing the `**/*` exclusion pattern. Without it, Immich imports and runs full ML processing (thumbnails, transcoding, CLIP, faces) on every target file itself, which duplicates all the work this sidecar exists to avoid. See [issue #3](https://github.com/donnchawp/immich-shared-library/issues/3).

> **Note:** The sidecar does not trigger library scans itself. It relies on Immich's library watching (or manual scans) to discover new source files. If library watching doesn't work in your environment (e.g., network drives), you can trigger scans manually:
> ```bash
> curl -X POST "http://localhost:2283/api/libraries/SOURCE_LIBRARY_ID/scan" \
>   -H "x-api-key: YOUR_KEY"
> ```

**Add to `.env`:**

```env
SOURCE_USER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TARGET_LIBRARY_ID=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz

# Path prefixes as seen inside the Immich container (not on the host)
SHARED_PATH_PREFIX=/external_library/user_a/shared/
TARGET_PATH_PREFIX=/external_library/user_b_shared/
```

#### Upload Sync

Syncs app/website uploads from a source user into the target user's account. Use this when the source user uploads photos via the Immich mobile app or web UI and you want them to appear in the target user's library too.

The source user (User C) uploads photos normally, so Immich stores them in its upload directory and runs ML processing. You create an external library for the target user (User B) containing a symlink to User C's upload directory. The sidecar creates asset records for User B directly in the database.

**Create the symlink** from a directory inside the target user's external library to the source user's upload directory:

```
/external_library/
  user_b_uploads/     # Target user's external library (NOT scanned, sidecar handles it)
    user_c -> /data/upload/cccccccc-cccc-cccc-cccc-cccccccccccc/
```

The symlink target must point to the source user's upload directory *inside the container*. Immich stores uploads at `{upload_location}/upload/{user_id}/`.

**Create the target external library in Immich** for the target user (User B). This must be a **separate library** from any library used for external library sync.

**Important:** This library must not be scanned by Immich. Add the exclusion pattern `**/*`:

1. Go to **Administration > External Libraries**
2. Click on the target user's new upload sync library
3. Add `**/*` as an exclusion pattern and save

> **Why a separate library?** The source user's libraries are actively scanned by Immich to discover and process new assets. The upload sync target library must *not* be scanned (Immich would create duplicate records and run redundant ML processing). The sidecar handles creating the asset records directly in the database.

> **⚠️ The same scan warning applies here:** disable Immich's periodic scan and never manually scan this library, or the `**/*` exclusion will cause Immich to offline every synced asset. See the warning in the external library sync setup above and [issue #3](https://github.com/donnchawp/immich-shared-library/issues/3).

**Add to `.env`:**

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

#### 4. Album Assignment (optional)

To have synced assets automatically added to an album in the target user's account, create the album first in Immich, then configure it.

**With config.yaml (recommended):** Add `album_id` to each job that needs it. Different jobs can target different albums:

```yaml
sync_jobs:
  - name: "alice-to-bob"
    # ...
    album_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # Bob's album
  - name: "charlie-to-alice"
    # ...
    album_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"  # Alice's album
```

**With env vars (legacy):** Add `TARGET_ALBUM_ID` to `.env`. This applies the same album to all jobs:

```env
TARGET_ALBUM_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
```

The album must be owned by the job's target user.

If you add an album after assets have already been synced, the sidecar will backfill all previously synced assets into the album on the next cycle. When a source asset is deleted, its album entry is also removed during cleanup.

#### 5. Start the Sidecar

The sidecar runs as a standalone compose project that connects to Immich's Docker network. From this directory:

```bash
docker compose up -d
```

> **Network configuration:** The `docker-compose.yml` assumes Immich's Docker network is named `immich_default` (the default when Immich's compose project is named `immich`). If your Immich setup uses a different network name (a custom `COMPOSE_PROJECT_NAME`, a different directory name, or a manually defined network), edit the `networks` section at the bottom of `docker-compose.yml` to match:
> ```yaml
> networks:
>   immich:
>     external: true
>     name: your_actual_network_name  # e.g., immich-app_default
> ```
> You can find your Immich network name with `docker network ls | grep immich`.

The `UPLOAD_LOCATION` and `EXTERNAL_LIBRARY_DIR` in `.env` must point to the same host directories that Immich mounts (typically `./library` and `./external_library` in your Immich directory). Hardlinks require the same filesystem.

The sidecar will:
- Wait for the Immich server to become available
- Create its tracking tables (`_face_sync_asset_map`, `_face_sync_meta`, `_face_sync_skipped`)
- Validate the schema and that all configured users share one cluster group
- Run a sync cycle every 60 seconds (configurable via `SYNC_INTERVAL_SECONDS`)

## Updating

When you pull new changes from this repository (for example after an Immich upgrade that requires sidecar schema fixes), rebuild the image and restart the container:

```bash
git pull
docker compose up -d --build
```

The `--build` flag is required. Without it, Docker keeps using the previously built image and your code changes never take effect.

Then check the logs to confirm the sidecar is running cleanly and syncing photos:

```bash
docker compose logs -f
```

Look for "Schema validation passed" on startup and periodic sync cycle messages. If schema validation fails, the logs will tell you exactly which columns or tables changed. That usually means Immich added new required columns that the sidecar needs to supply.

### Upgrading from Immich v3.1.x

If you are upgrading an instance that was already running an older version of this sidecar, the order
matters. Immich's cluster-group migration gives **every user its own new cluster group**, so the moment
you upgrade, your configured users no longer share one and the sidecar will refuse to start until you
join them.

Your existing synced data also needs a decision. The older sidecar mirrored each source person into a
private person row on the target user. Immich's migration turns every one of those mirrored persons
into its own person group, so the same human ends up split between the source's group and the target's
leftover mirror, and newly synced assets will use the source's group, leaving two entries for one
person that drift further apart every cycle. Resetting facial recognition for the group collapses that
split; nothing else will.

1. **Stop the sidecar** before upgrading Immich. It must not be writing while Immich runs its
   migrations.
2. **Back up the database** (see the `pg_dump` note under [Prerequisites](#prerequisites)). Also dump
   `_face_sync_person_map` on its own, because the sidecar drops that table on its first start and it is the
   only record of which target person mirrored which source person:
   ```bash
   docker exec immich_postgres pg_dump -U postgres -d immich -t _face_sync_person_map > person_map.sql
   ```
   This fails with "no matching tables were found" if you never ran a sidecar
   old enough to have mirrored persons. That's fine, there is nothing to save.
3. **Save your people.** The reset in step 5 deletes every person for every member of the group, names
   and birth dates included, and there is no undo. Export the names and copy each person's face
   thumbnail out first, so you can re-apply them afterwards:
   ```bash
   docker exec immich_postgres psql -U postgres -d immich --csv -c "
     SELECT u.name AS owner, p.name AS person_name, p.\"birthDate\", p.\"thumbnailPath\",
            (SELECT count(*) FROM asset_face af
              WHERE af.\"personId\" = p.id AND af.\"deletedAt\" IS NULL) AS face_count
       FROM person p JOIN \"user\" u ON u.id = p.\"ownerId\"
      WHERE p.name <> '' OR p.\"birthDate\" IS NOT NULL
      ORDER BY face_count DESC;" > named_people.csv
   ```
   Then `docker cp immich_server:<thumbnailPath> .` for each row. Without the thumbnails you are
   re-naming thousands of unlabelled clusters from memory.
4. **Upgrade Immich** to v3.2.0 and let its migrations finish.
5. **Start the sidecar once, before joining the group**, but only if you are coming from a sidecar older
   than schema v4. It runs `_migrate_v4()`, which strips the copied face embeddings, and then exits with
   a cluster-group error. That error is expected, and it is what makes this step safe: startup runs the
   migration first and validates the cluster group second, so the migration commits and the process dies
   before any sync can touch anything. Confirm you see both lines:
   ```
   Migrated tracking tables from v3 to v4
   ERROR ... do not share one cluster group
   ```
   Skip this step and every synced face still votes twice in the reset below, which invents people that
   should not exist. (If you had already reset before finding this out, see
   [prune_inflated_people.py](#utility-scripts), which undoes the damage after the fact.)
6. **Join the users into one cluster group** (Account Settings > Sharing > Cluster group), then run
   **Reset facial recognition** for that group and wait for the re-recognition job to finish. Joining
   alone is not enough: it preserves each member's separate person groups, so identity is still not
   shared.

   Watch for table bloat while it runs. Nulling every `personGroupId` leaves one dead tuple per face, and
   each reassignment adds another. Autovacuum reclaims them only if nothing pins the vacuum horizon. A
   stalled Immich sync stream (`state = active`, `wait_event = ClientRead`) will, and then every recognition
   query walks the dead index entries instead of the live ones. On a ~950k-face library that was the
   difference between 132 and 1,000 faces/minute. Check for it with:
   ```bash
   docker exec immich_postgres psql -U postgres -d immich -c "select pid, now()-xact_start as age,
     wait_event from pg_stat_activity where datname='immich' and xact_start < now() - interval '5 minutes';"
   ```
   Then `pg_terminate_backend(<pid>)`, followed by `VACUUM (ANALYZE) asset_face;`. Terminate rather than
   `pg_cancel_backend`: a backend parked in `ClientRead` has no running query to cancel.
7. **Rebuild and start the sidecar** (`docker compose up -d --build`). It validates the schema and the
   cluster group on startup and drops the retired `_face_sync_person_map` table.
8. **Re-apply the names** from your CSV.

Apart from the single migration-only start in step 5, leave the sidecar stopped until step 7.
Re-recognition rewrites `asset_face."personGroupId"` across the whole cluster group, and the sidecar's
face-reassignment pass has no reason to race it.

## Configuration Reference

### config.yaml (per-job settings)

Each job in `sync_jobs` supports these fields:

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique name for this sync job |
| `source_user_id` | Yes | UUID of the source user |
| `target_user_id` | Yes | UUID of the target user (receives synced copies) |
| `target_library_id` | Yes | UUID of the target user's external library (with `**/*` exclusion) |
| `source_path_prefix` | Yes | Path prefix for source assets inside the container |
| `target_path_prefix` | Yes | Path prefix for target assets inside the container |
| `album_id` | No | UUID of album to add synced assets to (must be owned by target user) |

### .env (infrastructure settings)

| Variable | Default | Description |
|---|---|---|
| `UPLOAD_LOCATION` | *(required)* | Host path to Immich's upload/data directory (e.g., `../immich-app/library`) |
| `EXTERNAL_LIBRARY_DIR` | *(required)* | Host path to the external library directory (e.g., `../immich-app/external_library`) |
| `DB_PASSWORD` | `postgres` | PostgreSQL password (same as Immich) |
| `DB_USERNAME` | `postgres` | PostgreSQL username |
| `DB_DATABASE_NAME` | `immich` | PostgreSQL database name |
| `IMMICH_API_KEY` | *(required)* | Immich API key |
| `SYNC_INTERVAL_SECONDS` | `60` | Seconds between sync cycles |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Legacy env vars (when config.yaml is not present)

When no `config.yaml` file exists, the sidecar falls back to these per-job env vars:

| Variable | Description |
|---|---|
| `TARGET_USER_ID` | UUID of the target user |
| `SOURCE_USER_ID` | UUID of the source user (external library sync) |
| `TARGET_LIBRARY_ID` | UUID of the target user's external library |
| `SHARED_PATH_PREFIX` | Source path prefix (external library sync) |
| `TARGET_PATH_PREFIX` | Target path prefix (external library sync) |
| `UPLOAD_SOURCE_USER_ID` | UUID of the source user (upload sync) |
| `UPLOAD_TARGET_LIBRARY_ID` | UUID of a separate external library (upload sync) |
| `TARGET_UPLOAD_PATH_PREFIX` | Target path prefix (upload sync) |
| `TARGET_ALBUM_ID` | UUID of album (applies to all jobs) |

At least one of `SHARED_PATH_PREFIX` or `UPLOAD_SOURCE_USER_ID` must be set. Both can be configured simultaneously.

## How the Sync Works

Each sync cycle runs six phases:

0. **Stale mapping prune**. Drops mappings whose target asset was hard-deleted in Immich, so the source syncs again next cycle. (Trashed targets keep their mapping.) It runs before anything else reads the map, so every later phase can assume its mapping points at a live asset. Run it any later and a hard-deleted target wedges the sidecar permanently: Phase 2 hits a foreign key violation inserting a face for the vanished asset, the cycle aborts, and the prune that would have fixed it never runs.
1. **New assets**. For each configured sync job (external library, uploads), finds fully-processed source assets not yet synced. Creates target asset records with copied EXIF, CLIP embeddings, faces, and hardlinked thumbnails.
1b. **Album assignment**. Adds newly synced assets to the target album (if configured). Backfills any previously synced assets that are missing from the album.
2. **Incremental faces**. Detects face updates on already-synced assets (using a watermark timestamp) and copies new faces.
3. **Person metadata**. Fills in empty target person names from source and hardlinks missing thumbnails. Visibility is not synced; see below.
4. **Cleanup**. Removes target assets (and their album entries) whose source was deleted or trashed. Reassigns target faces back to the source's `personGroupId` if they've drifted. Removes unnamed target persons left with no faces. Each of the three is savepointed separately: Phase 4 unlinks thumbnails before deleting the rows that name them, and the filesystem doesn't roll back.

## How Faces Are Handled

Under cluster groups, person identity (`personGroupId`) is shared between source and target, so there's nothing to mirror. When the sidecar syncs a face, it copies the `personGroupId` verbatim and, if the target user has no `person` row for that group yet, creates one (copying name, thumbnail, visibility, birth date from the source's row).

After that, per-cycle sync is asymmetric by design:
- **Names**: only fills an *empty* target name. If the target user has named the person themselves, the sidecar never overwrites it. Names are per-user in v3.2.0.
- **Visibility** (`isHidden`): inherited once when the target's `person` row is created, then owned by the target user. Nothing in a cycle overwrites it, so each user can show or hide the same person independently. Unlike `name`, a boolean has no "unset" value to test, so there is no fill-only middle ground. It is either synced or not, and it is not.
- **Face reassignment**: the source is authoritative. If the target user reassigns a synced face to a different person, the sidecar reverts it on the next cycle (matching by bounding box). This is the one place the source still wins over a target-side edit.

Because identity is shared rather than mirrored, there's no merge step and no duplicate-person cleanup to perform. A face either belongs to the same `person_group` on both accounts, or it doesn't.

## Caveats

- **`force=true` jobs**: If someone triggers a force re-process in Immich, it will re-run ML on the target user's assets, overwriting the copied data. The sidecar will re-sync on the next cycle, but there will be temporary GPU usage.
- **Same filesystem required**: Hardlinks only work when the sidecar container mounts the same volume as Immich. Cross-filesystem setups would need file copies instead.
- **Regenerating a target thumbnail writes through the hardlink**: Immich overwrites thumbnail files in place rather than writing a temp file and renaming, so any job that rewrites a target asset's thumbnail at the same path (a force re-process, a regenerate-thumbnails run) also rewrites the source user's copy, because they share an inode. Applying an edit is *not* affected: Immich writes the edited renders to separate `_edited` filenames, leaving the base thumbnails untouched.
- **Edits are copied once**: Crop/rotate/mirror history (`asset_edit`) is copied when the asset is first synced, so the target's `isEdited` flag and thumbnails agree. Edits made on the source *after* that aren't propagated, the same as EXIF and OCR data.
- **Direct database access**: This service writes directly to Immich's database. Tested with v3.2.0. Schema changes in other versions may require updates to this sidecar. Always back up your database before use.
- **Cluster group membership is enforced on startup**: the sidecar checks `user.clusterGroupId` for every configured user on startup (and again at the start of each cycle, once there's new work) and refuses to run if they don't match.
- **Copied faces are invisible to Immich's facial recognition, on purpose**: a copy gets no `face_search` row and `sourceType = 'manual'`. Without both, a cluster-group *Reset facial recognition* double-counts every shared face. A copied embedding is byte-identical to its source, so it sits at distance 0 and votes a second time when recognition checks `minFaces`. A person in two synced photos then becomes a person who should not exist. Sidecars before schema v4 did copy embeddings; `_migrate_v4()` strips them from existing copies on upgrade. The trade-off is that the target's faces can no longer be recognized independently, because the source owns their identity, so leaving the cluster group needs a face-detection re-run.
- **Single direction**: Sync is one-way (source → target). Changes made to target assets in Immich are not propagated back.

## Contributing

### Development Setup

This is optional. The sidecar runs entirely in Docker and `setup.py` uses only the Python standard library. A local venv is only useful for IDE autocomplete, linting, and syntax checking.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Utility Scripts

The utility scripts read configuration from `.env` (the same file used by `docker compose`). Since the Immich PostgreSQL container doesn't expose port 5432 by default, they must run inside a Docker container on the Immich network. `run-utility.sh` handles the Docker invocation:

```bash
./run-utility.sh test_sync.py
./run-utility.sh dedup_synced.py --match-time
./run-utility.sh delete_synced.py
```

- **`test_sync.py`**. Run a single sync cycle and print verification queries.
- **`delete_synced.py`**. Deletes all synced assets for a target user. Does not mark sources as skipped, so running the sync engine again will recreate everything. Useful for resetting a target account.
- **`dedup_synced.py`**. Detects and removes synced assets that duplicate the target user's own uploads (matched by filename + capture date). Use `--match-time` to compare the full timestamp (with TZ normalisation) instead of just the date. Marks duplicates as skipped so the sync engine won't recreate them.
- **`prune_inflated_people.py`**. Remediation for an instance that ran facial recognition *before* the sidecar reached schema v4. Copied faces used to carry a byte-identical `face_search` embedding, so every synced face was a distance-0 twin of its source and voted twice towards `minFaces`. Clusters cleared the threshold that shouldn't have, and became people who should not exist. `_migrate_v4()` strips the twins, but it can't undo a recognition run that already happened; this can. It judges each person group on its *original* faces alone (a face on an asset that is a target in `_face_sync_asset_map` is a copy) and deletes groups that fall below `minFaces`, leaving their faces unassigned exactly where the threshold would have left them. Named people are never touched, nor is any group that holds no copied face, so a legitimate small person, or one that has since shrunk, is safe. **Dry run by default: read the list of groups it prints before re-running with `--apply`, because the delete is irreversible.** Pass `--min-faces` matching your instance's setting; the script reads Immich's configured value and warns if you disagree with it.
- **`reset.sh`**. Full reset: stops the sidecar container, deletes all synced assets and mirrored persons from Immich, drops the tracking tables, and removes symlinks from the external library directory. Run directly on the host (not via `run-utility.sh`). Shows a summary and prompts for confirmation before making changes. Note it leaves the hardlinked thumbnail files behind. They cost no disk space, being hardlinks, but nothing afterwards knows their paths. `delete_synced.py` removes the files first and is the better tool if you want the filesystem clean.

`delete_synced.py` and `dedup_synced.py` are interactive: they show a summary and prompt for confirmation before making changes, with a dry-run option.

### Automated Tests

The project has an automated test suite (pytest) run against a scratch `immich_test` database built from a real Immich schema dump (`tests/fixtures/schema_v3.2.0.sql`):

```bash
make testdb   # (re)create the scratch database from the fixture
make test     # run the tests
```

`make help` lists all targets (`test`, `testdb`, `testdb-clean`, `schema-dump`, `lint`). Postgres isn't published to the host, so `make test` runs pytest inside a container on the `immich_default` network. Running `pytest` directly on the host will fail to connect. Narrow to one file with `make test PYTEST_ARGS=tests/test_person_sync.py`. If your Immich network or Postgres container is named differently, override them: `NETWORK=myproject_default PG=myproject-postgres-1 make test`. The defaults are `immich_default` and `immich_postgres`.

### Manual Integration Testing

`test_sync.py` is a manual integration script for exercising the sidecar against a real, live Immich instance. The automated tests above don't replace it for end-to-end checks against real data.

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

6. Run `test_sync.py` again. It recreates the deleted assets, which confirms the full round-trip works.

### Project Structure

```
src/
  main.py          - Entry point: config validation, tracking-table migrations, health check, concurrent loops
  sync_engine.py   - Orchestrates the 6-phase sync cycle
  asset_sync.py    - Asset record creation, EXIF copy, path remapping
  ml_sync.py       - Face and embedding sync (copies `personGroupId` verbatim)
  person_sync.py   - Target person creation, name/thumbnail sync, orphan cleanup
  album_sync.py    - Album assignment and backfill
  cleanup.py       - Deletion detection, face reassignment, and cleanup
  file_ops.py      - Hardlink creation and removal
  db.py            - asyncpg connection pool and transaction helpers
  config.py        - SyncJob dataclass, YAML loader, Pydantic Settings (env var fallback)
  schema.py        - Immich schema validation + cluster-group membership check
  immich_api.py    - Immich REST API client (health check)
  health.py        - TCP health check server
```

### Key Things to Know

- Immich tables are **singular** (`asset`, not `assets`) with **camelCase** columns that must be double-quoted in SQL.
- The sidecar creates tracking tables prefixed with `_face_sync_` to avoid colliding with Immich's schema: `_face_sync_asset_map`, `_face_sync_meta` (schema version), `_face_sync_skipped`.
- Each asset sync uses a PostgreSQL SAVEPOINT so one failure doesn't roll back the entire batch.
- Cleanup deletes hardlinked files before DB records to avoid orphan files on crash.
- Person identity (`personGroupId`) is shared across the cluster group, so the sidecar copies it verbatim rather than maintaining a source-to-target person mapping.
