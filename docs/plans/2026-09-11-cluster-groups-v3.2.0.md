# Immich v3.2.0 Cluster-Group Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the sidecar to Immich v3.2.0's cluster-group schema, replacing hand-rolled person mirroring with Immich's native shared `person_group` identity.

**Architecture:** In v3.2.0, face→person identity moved out of `person` into a shared `person_group` table scoped to a `cluster_group`. Each user holds their own `person` row keyed `(ownerId, personGroupId)` carrying only their name/birthdate/thumbnail. When source and target users share a cluster group, the sidecar copies `asset_face.personGroupId` **verbatim** — identity is shared by foreign key rather than by a mapping table. This deletes `_face_sync_person_map`, the canonical-resolution loop guard, merge adoption, and bounding-box merge detection.

**Tech Stack:** Python 3.11+, asyncpg, PostgreSQL 16 + pgvector, Docker, pytest (added by Task 1).

**Spec:** This plan is its own spec. Immich's schema changes are defined by `server/src/schema/migrations/1787148183729-ClusterGroups.ts` in the [v3.2.0 tag](https://github.com/immich-app/immich/blob/v3.2.0/server/src/schema/migrations/1787148183729-ClusterGroups.ts); behavioural facts are cited from `server/src/services/person.service.ts`, `server/src/repositories/person.repository.ts` and `server/src/cores/storage.core.ts` at the same tag.

## Current State (verified 2026-09-11)

The live instance at `../immich-app/` is **already on v3.2.0** — `IMMICH_VERSION=v3` is a floating tag and the `ClusterGroups` migration has already run against the live database. **The sidecar is broken right now** and will fail `validate_schema` on its next cycle.

Verified against the live DB:

| Check | Value |
|---|---|
| `person.id` | gone |
| `asset_face."personId"` | gone |
| `asset_face."personGroupId"` | present |
| `cluster_group`, `person_group`, `user."clusterGroupId"` | present |
| Users | `doc.test@z9.io` = `c7e0a257…` (source), `doc.jacinta@z9.io` = `9d3f2dc3…`, `doc.tester@z9.io` = `616e2d0b…` |
| Cluster groups | **three separate ones** — no users share a group yet |
| Assets | 26 (test), 22 (jacinta), 0 (tester) |
| Named persons | 2 (test), 4 (jacinta), 0 (tester) — **6 total to lose in the FR reset** |
| `_face_sync_asset_map` | 28 rows |
| `_face_sync_person_map` | 7 rows, all still resolving to valid `person_group` ids |
| Faces | 57, of which 29 assigned |
| Sidecar tables present | `_face_sync_asset_map`, `_face_sync_person_map`, `_face_sync_meta`, `_face_sync_skipped` |

The migration ran `UPDATE person SET "personGroupId" = "id"`, so old `person.id` values became `person_group.id` values. Nothing in the tracking tables is orphaned — source and target persons are simply still separate groups in separate cluster groups, which the FR reset in Task 8 will merge.

**Pin your Immich version.** `IMMICH_VERSION=v3` floats and will keep silently migrating your schema out from under the sidecar. Set `IMMICH_VERSION=v3.2.0` in `../immich-app/.env`.

## Global Constraints

- **Target Immich version: v3.2.0.** Do not preserve v3.1.0 compatibility — `person.id` no longer exists and cannot be conditionally supported.
- **Postgres is not published to the host.** Everything — psql, pytest — runs either via `docker exec immich_postgres` or in a container on the `immich_default` network reaching `immich_postgres:5432`.
- **Tests run against a separate `immich_test` database** in the same Postgres container. Never point tests at `immich`.
- **Precondition: all users named in `config.yaml` (every `source_user_id` and every `target_user_id`) must share one `cluster_group`.** `user.clusterGroupId` is a single NOT NULL uuid, so a user belongs to exactly one group. For this repo's config that is three users: `c7e0a257-c493-4f11-8672-9788e2c45943` (donncha), `9d3f2dc3-ac51-41b6-bf27-9625f7a892c7` (jacinta), `616e2d0b-facb-4184-8a4d-54937320661c` (tester).
- **Joining a cluster group requires a facial-recognition reset for every member.** All existing names and birth dates are lost. This is Immich's requirement, not the sidecar's.
- **Never run any Immich job with `force=true`** — it bypasses the skip logic the whole sidecar depends on.
- All Immich table names are singular; all column names are camelCase and must be double-quoted in SQL.
- Immich's `handleRecognizeFaces` skips any face where `personGroupId IS NOT NULL` (`person.service.ts:498-501`). Copied faces must always carry a non-null `personGroupId` where the source had one, or the target will re-run recognition.
- Immich's `deleteEmptyGroups` (`person.repository.ts:154-169`) deletes any `person_group` with **no `person` row**, and `asset_face.personGroupId` is `ON DELETE SET NULL`. The sidecar must always insert the target's `person` row, or a later Immich cleanup will null the copied faces.
- `python3`, not `python`, on macOS. Quick syntax check: `python3 -m py_compile src/<file>.py`.

### v3.2.0 schema facts this plan depends on

| Thing | v3.1.0 | v3.2.0 |
|---|---|---|
| `person` PK | `id` (uuid) | `(ownerId, personGroupId)` — **`id` column dropped** |
| Face→person link | `asset_face."personId"` → `person.id` | `asset_face."personGroupId"` → `person_group.id`, `ON DELETE SET NULL` |
| Person thumbnail path | `thumbs/{ownerId}/{personId[0:2]}/{personId[2:4]}/{personId}.jpeg` | `thumbs/{ownerId}/{personGroupId[0:2]}/{personGroupId[2:4]}/{personGroupId}.jpeg` |
| New tables | — | `cluster_group`, `cluster_group_request`, `person_group`, `person_group_audit` |
| `user` | — | `+ "clusterGroupId" uuid NOT NULL` |
| `person_audit` | `personId` | `personGroupId` |

`person` retains: `ownerId`, `personGroupId`, `createdAt`, `updatedAt`, `name`, `thumbnailPath`, `isHidden`, `birthDate`, `faceAssetId`, `isFavorite`, `color`, `updateId`. `faceAssetId` is still a FK to `asset_face.id`, so it must still point at a **target** face.

---

## File Structure

| File | Change | Responsibility after the port |
|---|---|---|
| `tests/conftest.py` | Create | pytest fixtures: scratch DB pool, per-test transaction rollback, factory helpers |
| `tests/fixtures/schema_v3.2.0.sql` | Create | Dumped v3.2.0 schema, used to build the scratch DB |
| `Makefile` | Create | `make test`, `make testdb`, `make lint` — repo currently has no Makefile |
| `src/schema.py` | Modify | Validate the v3.2.0 column set; add cluster-group precondition check |
| `src/person_sync.py` | Rewrite (422 → ~120 lines) | Ensure target `person` row exists per `(ownerId, personGroupId)`; hardlink thumbnail; sync name/visibility/thumbnail |
| `src/ml_sync.py` | Modify | Copy `personGroupId` verbatim; set `faceAssetId` by composite key |
| `src/cleanup.py` | Modify | Propagate source face reassignment as a single UPDATE; simplify orphan person cleanup |
| `src/asset_sync.py` | Modify | Drop `_face_sync_person_map` creation; keep `_face_sync_asset_map` |
| `src/sync_engine.py` | Modify | Drop `cleanup_reassigned_faces`' person plumbing; rename stats keys |
| `README.md`, `CLAUDE.md` | Modify | Document v3.2.0 requirement and the cluster-group precondition |

---

## Task 1: Scratch v3.2.0 database and test harness

Nothing else in this plan can be verified without a real v3.2.0 schema. Your live instance is already on v3.2.0, so this task dumps its schema into a separate `immich_test` database and stands up pytest.

**Files:**
- Create: `Makefile`
- Create: `tests/conftest.py`
- Create: `tests/test_harness.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: pytest fixture `conn` (an `asyncpg.Connection` inside a transaction that is rolled back after each test); factory helpers `make_cluster_group(conn) -> UUID`, `make_user(conn, *, cluster_group_id=None) -> UUID`, `make_asset(conn, owner_id, *, original_path=None) -> UUID`, `make_person_group(conn, cluster_group_id) -> UUID`, `make_person(conn, owner_id, person_group_id, *, name="") -> None`, `make_face(conn, asset_id, *, person_group_id=None, bbox=(0,0,10,10)) -> UUID`.

- [ ] **Step 1: Add pytest to the project**

Add to `pyproject.toml` after the `dependencies` block:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Capture the live v3.2.0 schema as a fixture**

Your live instance is already on v3.2.0, so the schema is dumped from it directly. `--schema-only` copies no photo data.

```bash
mkdir -p tests/fixtures
docker exec immich_postgres pg_dump -U postgres --schema-only immich \
  > tests/fixtures/schema_v3.2.0.sql
grep -c 'CREATE TABLE public.person_group' tests/fixtures/schema_v3.2.0.sql
```

Expected: `2` — one match for `person_group`, one for `person_group_audit`. *(Already done: the fixture is 4,726 lines.)*

- [ ] **Step 3: Create the scratch database**

A second database inside the same container. The live `immich` database is never touched.

```bash
docker exec immich_postgres psql -U postgres -c "DROP DATABASE IF EXISTS immich_test;"
docker exec immich_postgres psql -U postgres -c "CREATE DATABASE immich_test;"
docker exec -i immich_postgres psql -U postgres -q -d immich_test < tests/fixtures/schema_v3.2.0.sql
```

- [ ] **Step 4: Verify the scratch schema is v3.2.0**

```bash
docker exec immich_postgres psql -U postgres -d immich_test -tAc "
SELECT
  (SELECT count(*) FROM information_schema.tables  WHERE table_name='person_group') AS pg_tbl,
  (SELECT count(*) FROM information_schema.columns WHERE table_name='person' AND column_name='id') AS person_id,
  (SELECT count(*) FROM information_schema.tables  WHERE table_name='_face_sync_asset_map') AS asset_map,
  (SELECT count(*) FROM person) AS person_rows;"
```

Expected: `1|0|1|0` — person_group exists, `person.id` is gone, the sidecar tracking table came across, and the database is empty. Anything else and every later task is built on a false premise. Stop here. *(Verified.)*

- [ ] **Step 5: Write the Makefile**

Postgres is not published to the host, so pytest runs in a container on `immich_default`.

```makefile
PG          := immich_postgres
NETWORK     := immich_default
TEST_DB_URL := postgresql://postgres:postgres@$(PG):5432/immich_test

.PHONY: help test testdb testdb-clean lint schema-dump

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

schema-dump:  ## Re-dump the live Immich schema into the test fixture
	docker exec $(PG) pg_dump -U postgres --schema-only immich > tests/fixtures/schema_v3.2.0.sql

testdb:  ## (Re)create the scratch test database from the fixture
	docker exec $(PG) psql -U postgres -c "DROP DATABASE IF EXISTS immich_test;"
	docker exec $(PG) psql -U postgres -c "CREATE DATABASE immich_test;"
	docker exec -i $(PG) psql -U postgres -q -d immich_test < tests/fixtures/schema_v3.2.0.sql

testdb-clean:  ## Drop the scratch test database
	docker exec $(PG) psql -U postgres -c "DROP DATABASE IF EXISTS immich_test;"

test:  ## Run the tests in a container on the Immich network (PYTEST_ARGS=tests/x.py to narrow)
	docker run --rm --network $(NETWORK) -v $(PWD):/app -w /app \
	  -e TEST_DB_URL=$(TEST_DB_URL) python:3.12-slim \
	  bash -c 'pip install -q asyncpg pytest pytest-asyncio && python -m pytest -v $(PYTEST_ARGS)'

lint:  ## Syntax-check all source files
	python3 -m py_compile src/*.py
```

- [ ] **Step 6: Write the fixtures**

Create `tests/conftest.py`:

```python
import os
from uuid import UUID, uuid4

import asyncpg
import pytest_asyncio

TEST_DB_URL = os.environ.get(
    "TEST_DB_URL", "postgresql://postgres:postgres@immich_postgres:5432/immich_test"
)


@pytest_asyncio.fixture
async def conn():
    """A connection inside a transaction that is always rolled back.

    Every test starts from the same clean schema and leaves no trace, so tests
    can run in any order without cleanup code.

    Deliberately not a session-scoped pool: pytest-asyncio gives each test its
    own event loop, and an asyncpg pool bound to one loop cannot be used from
    another ("attached to a different loop"). A connection per test is cheap at
    this suite size and sidesteps the whole problem.
    """
    connection = await asyncpg.connect(TEST_DB_URL)
    tx = connection.transaction()
    await tx.start()
    try:
        yield connection
    finally:
        await tx.rollback()
        await connection.close()


async def make_cluster_group(conn) -> UUID:
    return await conn.fetchval(
        'INSERT INTO cluster_group (id) VALUES ($1) RETURNING id', uuid4()
    )


async def make_user(conn, *, cluster_group_id: UUID | None = None) -> UUID:
    if cluster_group_id is None:
        cluster_group_id = await make_cluster_group(conn)
    uid = uuid4()
    await conn.execute(
        'INSERT INTO "user" (id, email, name, "clusterGroupId") VALUES ($1, $2, $3, $4)',
        uid, f"{uid}@test.local", str(uid)[:8], cluster_group_id,
    )
    return uid


async def make_person_group(conn, cluster_group_id: UUID) -> UUID:
    return await conn.fetchval(
        'INSERT INTO person_group (id, "clusterGroupId") VALUES ($1, $2) RETURNING id',
        uuid4(), cluster_group_id,
    )


async def make_person(conn, owner_id: UUID, person_group_id: UUID, *, name: str = "") -> None:
    await conn.execute(
        'INSERT INTO person ("ownerId", "personGroupId", name) VALUES ($1, $2, $3)',
        owner_id, person_group_id, name,
    )


async def make_asset(conn, owner_id: UUID, *, original_path: str | None = None) -> UUID:
    # "checksumAlgorithm" is NOT NULL with no default in v3.2.0 (enum: sha1 | sha1-path).
    aid = uuid4()
    await conn.execute(
        """
        INSERT INTO asset (id, "ownerId", "originalPath", "originalFileName",
                           checksum, "checksumAlgorithm", type,
                           "fileCreatedAt", "fileModifiedAt", "localDateTime")
        VALUES ($1, $2, $3, $4, $5, 'sha1', 'IMAGE', NOW(), NOW(), NOW())
        """,
        aid, owner_id, original_path or f"/external_library/{aid}.jpg",
        f"{aid}.jpg", aid.bytes,
    )
    return aid


async def make_face(conn, asset_id: UUID, *, person_group_id: UUID | None = None,
                    bbox: tuple[int, int, int, int] = (0, 0, 10, 10)) -> UUID:
    fid = uuid4()
    x1, y1, x2, y2 = bbox
    await conn.execute(
        """
        INSERT INTO asset_face (id, "assetId", "personGroupId", "imageWidth", "imageHeight",
                                "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2")
        VALUES ($1, $2, $3, 100, 100, $4, $5, $6, $7)
        """,
        fid, asset_id, person_group_id, x1, y1, x2, y2,
    )
    return fid
```

> **If `make_asset` or `make_user` raises `null value in column "..." violates not-null constraint`,** the v3.2.0 table has a NOT NULL column these factories don't supply (`asset.visibility` and `user.profileImagePath` are the likely candidates). Find them and add them to the factory — do not make the column nullable:
>
> ```bash
> docker exec immich_postgres psql -U postgres -d immich_test -c \
>   "SELECT column_name, column_default FROM information_schema.columns \
>    WHERE table_name='asset' AND is_nullable='NO' AND column_default IS NULL;"
> ```

- [ ] **Step 7: Write a harness smoke test**

Create `tests/test_harness.py`:

```python
from tests.conftest import make_cluster_group, make_face, make_asset, make_person, make_person_group, make_user


async def test_schema_is_v3_2_0(conn):
    """person.id is gone and asset_face.personGroupId exists."""
    person_id_col = await conn.fetchval(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'person' AND column_name = 'id'"
    )
    assert person_id_col is None

    face_col = await conn.fetchval(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'asset_face' AND column_name = 'personGroupId'"
    )
    assert face_col == "personGroupId"


async def test_two_users_can_share_one_person_group(conn):
    """The central premise: one person_group, two person rows, two owners."""
    cg = await make_cluster_group(conn)
    alice = await make_user(conn, cluster_group_id=cg)
    bob = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    await make_person(conn, alice, pg, name="Mum")
    await make_person(conn, bob, pg, name="Mam")

    names = await conn.fetch(
        'SELECT "ownerId", name FROM person WHERE "personGroupId" = $1 ORDER BY name', pg
    )
    assert [r["name"] for r in names] == ["Mam", "Mum"]


async def test_faces_from_both_users_share_the_group(conn):
    cg = await make_cluster_group(conn)
    alice = await make_user(conn, cluster_group_id=cg)
    bob = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    a_asset = await make_asset(conn, alice)
    b_asset = await make_asset(conn, bob)
    await make_face(conn, a_asset, person_group_id=pg)
    await make_face(conn, b_asset, person_group_id=pg)

    count = await conn.fetchval(
        'SELECT COUNT(*) FROM asset_face WHERE "personGroupId" = $1', pg
    )
    assert count == 2
```

- [ ] **Step 8: Run the tests**

```bash
make testdb-clean 2>/dev/null || true
make testdb
make test
```

Expected: 5 passed. `test_schema_is_v3_2_0` is the load-bearing one — if it fails, the scratch DB is not on v3.2.0.

- [ ] **Step 9: Commit**

```bash
git add Makefile pyproject.toml tests/
git commit -m "test: add v3.2.0 scratch database and pytest harness

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 2: Schema validation and cluster-group precondition

`schema.py` is the sidecar's tripwire — it must fail loudly on a wrong schema and on users who are not in the same cluster group.

**Files:**
- Modify: `src/schema.py:69-72` (REQUIRED_SCHEMA person), `src/schema.py:156-159` (INSERTED_COLUMNS person)
- Create: `tests/test_schema.py`

**Interfaces:**
- Consumes: `conn` fixture and factories from Task 1.
- Produces: `validate_cluster_group(conn, user_ids: list[UUID]) -> None` in `src/schema.py`, raising `SchemaValidationError` when the users do not share one `clusterGroupId`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schema.py`:

```python
import pytest

from src.schema import SchemaValidationError, validate_cluster_group
from tests.conftest import make_cluster_group, make_user


async def test_same_cluster_group_passes(conn):
    cg = await make_cluster_group(conn)
    alice = await make_user(conn, cluster_group_id=cg)
    bob = await make_user(conn, cluster_group_id=cg)

    await validate_cluster_group(conn, [alice, bob])  # must not raise


async def test_different_cluster_groups_raise(conn):
    alice = await make_user(conn)  # own cluster group
    bob = await make_user(conn)    # different cluster group

    with pytest.raises(SchemaValidationError, match="cluster group"):
        await validate_cluster_group(conn, [alice, bob])


async def test_missing_user_raises(conn):
    from uuid import uuid4
    alice = await make_user(conn)

    with pytest.raises(SchemaValidationError, match="not found"):
        await validate_cluster_group(conn, [alice, uuid4()])
```

- [ ] **Step 2: Run to verify it fails**

Run: `make test` (or `TEST_DB_URL=... python3 -m pytest tests/test_schema.py -v`)
Expected: FAIL with `ImportError: cannot import name 'validate_cluster_group'`.

- [ ] **Step 3: Update the person column sets**

In `src/schema.py`, replace the `"person"` entry in `REQUIRED_SCHEMA` (currently lines 69-72):

```python
    "person": {
        "ownerId", "personGroupId", "name", "thumbnailPath", "isHidden",
        "birthDate", "faceAssetId", "isFavorite", "color",
    },
    "person_group": {
        "id", "clusterGroupId",
    },
    "cluster_group": {
        "id",
    },
```

Replace the `"person"` entry in `INSERTED_COLUMNS` (currently lines 156-159) with the same column set minus `faceAssetId` (the sidecar sets that in a later UPDATE, not on INSERT):

```python
    "person": {
        "ownerId", "personGroupId", "name", "thumbnailPath", "isHidden",
        "birthDate", "isFavorite", "color",
    },
```

Add `"clusterGroupId"` to the `"user"` entry in `REQUIRED_SCHEMA` (currently line 85-87):

```python
    "user": {
        "id", "deletedAt", "clusterGroupId",
    },
```

- [ ] **Step 4: Add the cluster-group check**

Append to `src/schema.py`:

```python
async def validate_cluster_group(conn: asyncpg.Connection, user_ids: list[UUID]) -> None:
    """Assert every user shares one cluster group.

    The sidecar copies ``asset_face."personGroupId"`` verbatim from source to
    target. A ``person_group`` belongs to exactly one ``cluster_group``, and
    Immich scopes face search and recognition by the acting user's
    ``clusterGroupId``. If the users are in different cluster groups the copied
    identity is invisible to the target and Immich's cleanup will eventually
    null the faces, so refuse to start.
    """
    rows = await conn.fetch(
        'SELECT id, "clusterGroupId" FROM "user" WHERE id = ANY($1)',
        list(user_ids),
    )
    found = {row["id"]: row["clusterGroupId"] for row in rows}

    missing = [str(uid) for uid in user_ids if uid not in found]
    if missing:
        raise SchemaValidationError(
            f"Configured user(s) not found in Immich: {', '.join(missing)}"
        )

    groups = set(found.values())
    if len(groups) > 1:
        detail = ", ".join(f"{uid}={found[uid]}" for uid in user_ids)
        raise SchemaValidationError(
            "All configured users must share one cluster group so face identity "
            f"can be copied between them. Found {len(groups)} groups: {detail}. "
            "Fix this in Immich under Account Settings > Sharing > Cluster group. "
            "Note that joining a group requires resetting facial recognition for "
            "every member, which discards existing names and birth dates."
        )
```

Add `from uuid import UUID` to the imports at the top of `src/schema.py` if it is not already present.

- [ ] **Step 5: Run the tests**

Run: `make test`
Expected: PASS, 6 total (3 from Task 1, 3 new).

- [ ] **Step 6: Wire the check into startup**

In `src/sync_engine.py`, inside `run_full_sync`, replace the existing validation call at line 43-45:

```python
                if source_assets and not schema_validated:
                    await validate_schema(conn)
                    await validate_cluster_group(conn, _configured_user_ids())
                    schema_validated = True
```

Add near the top of `src/sync_engine.py`:

```python
from src.schema import validate_cluster_group, validate_schema


def _configured_user_ids() -> list[UUID]:
    """Every distinct user the sidecar touches, across all jobs."""
    ids: list[UUID] = []
    for job in settings.sync_jobs:
        for uid in (job.source_user_id, job.target_user_id):
            if uid not in ids:
                ids.append(uid)
    return ids
```

Make the same pairing in `src/main.py` wherever `validate_schema` is called at startup.

- [ ] **Step 7: Syntax check and commit**

```bash
make lint
git add src/schema.py src/sync_engine.py src/main.py tests/test_schema.py
git commit -m "feat: validate v3.2.0 person schema and cluster-group membership

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 3: Rewrite person_sync.py around shared person groups

This is the heart of the port. 422 lines collapse to roughly 120.

**Files:**
- Modify: `src/person_sync.py` (full rewrite)
- Create: `tests/test_person_sync.py`

**Interfaces:**
- Consumes: `validate_cluster_group` exists (Task 2); fixtures from Task 1.
- Produces:
  - `ensure_target_person(conn, person_group_id: UUID, source_user_id: UUID, target_user_id: UUID) -> UUID | None` — creates the target's `person` row for the group if absent, copying name/birthDate/isHidden/color and hardlinking the thumbnail. Returns `person_group_id` on success, `None` if the source has no `person` row for that group.
  - `sync_person_names(conn) -> int`
  - `sync_person_visibility(conn) -> int`
  - `sync_person_thumbnails(conn) -> int`
  - `cleanup_orphaned_persons(conn) -> int`
  - `_hardlink_person_thumbnail(person_group_id, target_user_id, source_thumbnail_path) -> str`

**Deleted:** `_resolve_canonical_person`, `_try_adopt_surviving_person`, `_check_mapping`, `get_or_create_target_person`, and the `_face_sync_person_map` table.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_person_sync.py`:

```python
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)
from src.person_sync import cleanup_orphaned_persons, ensure_target_person, sync_person_names


async def test_creates_target_person_row_for_shared_group(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")

    result = await ensure_target_person(conn, pg, src, tgt)

    assert result == pg
    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Granny"


async def test_is_idempotent(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")

    await ensure_target_person(conn, pg, src, tgt)
    await ensure_target_person(conn, pg, src, tgt)

    count = await conn.fetchval(
        'SELECT COUNT(*) FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert count == 1


async def test_does_not_overwrite_a_name_the_target_already_set(conn):
    """The target user's own naming wins — we only fill an empty name."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Granny")
    await make_person(conn, tgt, pg, name="Nana")

    await ensure_target_person(conn, pg, src, tgt)
    await sync_person_names(conn)

    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Nana"


async def test_returns_none_when_source_has_no_person_row(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)  # no person row for src

    assert await ensure_target_person(conn, pg, src, tgt) is None


async def test_cleanup_removes_target_person_with_no_faces(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)  # target person, source person gone, no faces

    removed = await cleanup_orphaned_persons(conn)

    assert removed == 1


async def test_cleanup_keeps_target_person_that_still_has_faces(conn):
    """Deleting a person whose faces remain would null those faces."""
    cg = await make_cluster_group(conn)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, tgt, pg)
    asset = await make_asset(conn, tgt)
    await make_face(conn, asset, person_group_id=pg)

    removed = await cleanup_orphaned_persons(conn)

    assert removed == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `make test PYTEST_ARGS=tests/test_person_sync.py`
Expected: FAIL with `ImportError: cannot import name 'ensure_target_person'`.

- [ ] **Step 3: Rewrite person_sync.py**

Replace the entire contents of `src/person_sync.py`:

```python
import logging
import os
from pathlib import Path
from uuid import UUID

import asyncpg

from src.config import settings
from src.file_ops import validate_path_within_upload

logger = logging.getLogger(__name__)


def _hardlink_person_thumbnail(
    person_group_id: UUID,
    target_user_id: UUID,
    source_thumbnail_path: str,
) -> str:
    """Hardlink a person's cropped face thumbnail into the target user's directory.

    Since v3.2.0 person thumbnails are keyed on the *person group*, not the
    person: /data/thumbs/{ownerId}/{pgid[0:2]}/{pgid[2:4]}/{pgid}.jpeg
    Source and target share the group id, so only the owner directory changes.
    """
    if not source_thumbnail_path:
        return ""

    upload_base = Path(settings.upload_location_mount)
    source = Path(source_thumbnail_path)

    try:
        validate_path_within_upload(source)
    except ValueError:
        logger.error("Source person thumbnail escapes upload directory: %s", source)
        return ""

    if not source.exists():
        logger.warning("Source person thumbnail does not exist: %s", source)
        return ""

    pgid = str(person_group_id)
    target_dir = upload_base / "thumbs" / str(target_user_id) / pgid[:2] / pgid[2:4]
    target = target_dir / f"{pgid}{source.suffix}"

    try:
        validate_path_within_upload(target)
    except ValueError:
        logger.error("Target person thumbnail escapes upload directory: %s", target)
        return ""

    target_dir.mkdir(parents=True, exist_ok=True)

    if target.exists():
        logger.debug("Target person thumbnail already exists: %s", target)
    else:
        try:
            os.link(str(source), str(target))
            logger.debug("Hardlinked person thumbnail %s -> %s", source, target)
        except OSError as e:
            logger.error("Failed to hardlink person thumbnail: %s", e)
            return ""

    return str(target)


async def ensure_target_person(
    conn: asyncpg.Connection,
    person_group_id: UUID,
    source_user_id: UUID,
    target_user_id: UUID,
) -> UUID | None:
    """Ensure the target user has a ``person`` row for this shared group.

    Under cluster groups the identity itself (``person_group``) is shared, so
    there is nothing to mirror — only the target's own name/thumbnail row to
    create. Immich creates this row lazily during facial recognition
    (person.service.ts:548), which the sidecar deliberately skips, so we must
    create it ourselves. Without it Immich's ``deleteEmptyGroups`` would later
    drop the group and null the copied faces.

    Returns ``person_group_id`` on success, or ``None`` if the source user has
    no person row for this group (nothing to copy a name from).
    """
    source = await conn.fetchrow(
        'SELECT name, "thumbnailPath", "isHidden", "birthDate", color '
        'FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        source_user_id,
        person_group_id,
    )
    if source is None:
        logger.debug(
            "Source user %s has no person row for group %s", source_user_id, person_group_id
        )
        return None

    already = await conn.fetchval(
        'SELECT 1 FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        target_user_id,
        person_group_id,
    )
    if already:
        return person_group_id

    target_thumbnail = _hardlink_person_thumbnail(
        person_group_id=person_group_id,
        target_user_id=target_user_id,
        source_thumbnail_path=source["thumbnailPath"],
    )

    # The composite PK makes this race-safe without an advisory lock: a
    # concurrent transaction inserting the same (ownerId, personGroupId) loses
    # the conflict and we keep whichever row landed first.
    await conn.execute(
        """
        INSERT INTO person ("ownerId", "personGroupId", name, "thumbnailPath",
                            "isHidden", "birthDate", "isFavorite", color)
        VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)
        ON CONFLICT ("ownerId", "personGroupId") DO NOTHING
        """,
        target_user_id,
        person_group_id,
        source["name"],
        target_thumbnail,
        source["isHidden"],
        source["birthDate"],
        source["color"],
    )

    logger.info(
        "Created person for user %s in group %s (name=%r)",
        target_user_id, person_group_id, source["name"],
    )
    return person_group_id


async def sync_person_names(conn: asyncpg.Connection) -> int:
    """Copy source person names onto target persons that have no name yet.

    Only fills empty names. Names are per-user in v3.2.0 by design — if the
    target user has named someone themselves, that is their choice and the
    sidecar must not stomp it.
    """
    updated = await conn.fetch(
        """
        UPDATE person t
        SET name = s.name
        FROM person s, _face_sync_asset_map m
        WHERE s."personGroupId" = t."personGroupId"
          AND s."ownerId" = m.source_user_id
          AND t."ownerId" = m.target_user_id
          AND s.name <> ''
          AND (t.name = '' OR t.name IS NULL)
        RETURNING t."ownerId", t."personGroupId", t.name
        """,
    )

    for row in updated:
        logger.info(
            "Named person group %s for user %s: %r",
            row["personGroupId"], row["ownerId"], row["name"],
        )

    return len(updated)


async def sync_person_visibility(conn: asyncpg.Connection) -> int:
    """Copy source ``isHidden`` onto target persons in the same group."""
    updated = await conn.fetch(
        """
        UPDATE person t
        SET "isHidden" = s."isHidden"
        FROM person s, _face_sync_asset_map m
        WHERE s."personGroupId" = t."personGroupId"
          AND s."ownerId" = m.source_user_id
          AND t."ownerId" = m.target_user_id
          AND t."isHidden" IS DISTINCT FROM s."isHidden"
        RETURNING t."personGroupId"
        """,
    )
    return len(updated)


async def sync_person_thumbnails(conn: asyncpg.Connection) -> int:
    """Hardlink thumbnails for target persons that still have none."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT t."ownerId" AS target_user_id,
               t."personGroupId" AS person_group_id,
               s."thumbnailPath" AS source_thumb
        FROM person t
        JOIN _face_sync_asset_map m ON m.target_user_id = t."ownerId"
        JOIN person s ON s."personGroupId" = t."personGroupId"
            AND s."ownerId" = m.source_user_id
        WHERE s."thumbnailPath" <> ''
          AND (t."thumbnailPath" = '' OR t."thumbnailPath" IS NULL)
        """,
    )

    count = 0
    for row in rows:
        target_thumb = _hardlink_person_thumbnail(
            person_group_id=row["person_group_id"],
            target_user_id=row["target_user_id"],
            source_thumbnail_path=row["source_thumb"],
        )
        if target_thumb:
            await conn.execute(
                'UPDATE person SET "thumbnailPath" = $1 '
                'WHERE "ownerId" = $2 AND "personGroupId" = $3',
                target_thumb, row["target_user_id"], row["person_group_id"],
            )
            count += 1

    return count


async def cleanup_orphaned_persons(conn: asyncpg.Connection) -> int:
    """Remove sidecar-created person rows that no longer have any faces.

    Safety: only delete a person row with NO remaining faces in its group owned
    by that user. Immich sets ``asset_face."personGroupId"`` to NULL when the
    last person row in a group goes (``deleteEmptyGroups``), so deleting a row
    whose faces survive would silently unassign them.
    """
    deleted = await conn.fetch(
        """
        DELETE FROM person t
        USING _face_sync_asset_map m
        WHERE t."ownerId" = m.target_user_id
          AND NOT EXISTS (
              SELECT 1 FROM person s
              WHERE s."personGroupId" = t."personGroupId"
                AND s."ownerId" = m.source_user_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM asset_face af
              JOIN asset a ON a.id = af."assetId"
              WHERE af."personGroupId" = t."personGroupId"
                AND a."ownerId" = t."ownerId"
                AND af."deletedAt" IS NULL
          )
        RETURNING t."personGroupId"
        """,
    )

    if deleted:
        logger.info("Cleaned up %d orphaned target persons", len(deleted))

    return len(deleted)
```

- [ ] **Step 4: Run the tests**

Run: `make test PYTEST_ARGS=tests/test_person_sync.py`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/person_sync.py tests/test_person_sync.py
git commit -m "refactor: replace person mirroring with shared person groups

Cluster groups make person identity shared by foreign key, so the canonical
resolution loop guard, merge adoption and mapping table are all unnecessary.

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 4: Copy personGroupId verbatim in ml_sync.py

**Files:**
- Modify: `src/ml_sync.py:6` (import), `:36-44` (person resolution), `:49-78` (INSERT), `:95-108` (faceAssetId)
- Create: `tests/test_ml_sync.py`

**Interfaces:**
- Consumes: `ensure_target_person` from Task 3.
- Produces: `sync_faces_for_asset(conn, source_asset_id, target_asset_id, source_user_id, target_user_id) -> int` — unchanged signature, new behaviour.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ml_sync.py`:

```python
from src.ml_sync import sync_faces_for_asset
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)


async def test_copies_person_group_id_verbatim(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 2, 3, 4))

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 1
    copied = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied == pg


async def test_creates_the_target_person_row(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg)

    await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Dad"


async def test_unassigned_face_copies_with_null_group(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=None)

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 1
    copied = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert copied is None


async def test_does_not_duplicate_an_existing_bounding_box(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, bbox=(5, 5, 15, 15))
    await make_face(conn, tgt_asset, bbox=(5, 5, 15, 15))  # already there

    count = await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    assert count == 0
    total = await conn.fetchval(
        'SELECT COUNT(*) FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert total == 1


async def test_sets_face_asset_id_on_the_target_person(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="Dad")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await make_face(conn, src_asset, person_group_id=pg)

    await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt)

    face_asset_id = await conn.fetchval(
        'SELECT "faceAssetId" FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2',
        tgt, pg,
    )
    target_face_id = await conn.fetchval(
        'SELECT id FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert face_asset_id == target_face_id
```

- [ ] **Step 2: Run to verify it fails**

Run: `make test PYTEST_ARGS=tests/test_ml_sync.py`
Expected: FAIL — `asset_face` has no column `personId` (the current code still inserts it).

- [ ] **Step 3: Update the import**

In `src/ml_sync.py`, replace line 6:

```python
from src.person_sync import ensure_target_person
```

- [ ] **Step 4: Replace the person resolution and INSERT**

In `src/ml_sync.py`, replace lines 36-78 (the body of the `for face in source_faces:` loop up to and including the INSERT) with:

```python
    for face in source_faces:
        source_face_id = face["id"]
        person_group_id = face["personGroupId"]

        # Identity is shared: the group id copies verbatim. We only need to make
        # sure the target user has their own person row on that group, so
        # Immich's deleteEmptyGroups doesn't drop it and null these faces.
        if person_group_id is not None:
            if await ensure_target_person(
                conn, person_group_id, source_user_id, target_user_id,
            ) is None:
                # Source has no person row for this group; copy the face
                # unassigned rather than pointing at a group that may vanish.
                person_group_id = None

        # Insert face record only if no matching bounding box exists on the target
        # asset (atomic check-and-insert to avoid TOCTOU race)
        target_face_id = uuid4()
        result = await conn.execute(
            """
            INSERT INTO asset_face (
                id, "assetId", "personGroupId",
                "imageWidth", "imageHeight",
                "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
                "sourceType", "isVisible"
            )
            SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
            WHERE NOT EXISTS (
                SELECT 1 FROM asset_face
                WHERE "assetId" = $2
                  AND "boundingBoxX1" = $6
                  AND "boundingBoxY1" = $7
                  AND "boundingBoxX2" = $8
                  AND "boundingBoxY2" = $9
            )
            """,
            target_face_id,
            target_asset_id,
            person_group_id,
            face["imageWidth"],
            face["imageHeight"],
            face["boundingBoxX1"],
            face["boundingBoxY1"],
            face["boundingBoxX2"],
            face["boundingBoxY2"],
            face["sourceType"],
            face["isVisible"],
        )
        if result == "INSERT 0 0":
            continue
```

- [ ] **Step 5: Replace the faceAssetId update**

In `src/ml_sync.py`, replace the `faceAssetId` block (was lines 95-108) with:

```python
        # Point the target person's feature photo at a face the target owns.
        # faceAssetId is still an FK to asset_face.id, so it must never
        # reference the source user's face row.
        if person_group_id is not None:
            await conn.execute(
                """
                UPDATE person SET "faceAssetId" = $1
                WHERE "ownerId" = $2 AND "personGroupId" = $3 AND (
                    "faceAssetId" IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM asset_face WHERE id = person."faceAssetId"
                    )
                )
                """,
                target_face_id,
                target_user_id,
                person_group_id,
            )
```

- [ ] **Step 6: Run the tests**

Run: `make test PYTEST_ARGS=tests/test_ml_sync.py`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add src/ml_sync.py tests/test_ml_sync.py
git commit -m "feat: copy asset_face.personGroupId verbatim between users

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 5: Simplify face reassignment propagation in cleanup.py

Source-side reassignment still needs propagating, but with shared identity it is one UPDATE rather than a mapping-table join plus per-row person resolution.

**Files:**
- Modify: `src/cleanup.py:6` (import), `:99-160ish` (`cleanup_reassigned_faces`)
- Create: `tests/test_cleanup.py`

**Interfaces:**
- Consumes: nothing from Task 4 at runtime; shares the schema.
- Produces: `cleanup_reassigned_faces(conn) -> int` — unchanged signature, no longer needs `get_or_create_target_person`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cleanup.py`:

```python
from src.cleanup import cleanup_reassigned_faces
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)


async def _link(conn, src_asset, tgt_asset, src_user, tgt_user):
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        src_asset, tgt_asset, src_user, tgt_user,
    )


async def test_propagates_a_source_reassignment(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    old_pg = await make_person_group(conn, cg)
    new_pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)

    # Source face now points at new_pg; target still has old_pg
    await make_face(conn, src_asset, person_group_id=new_pg, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=old_pg, bbox=(1, 1, 9, 9))

    updated = await cleanup_reassigned_faces(conn)

    assert updated == 1
    now = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert now == new_pg


async def test_is_a_no_op_when_already_in_sync(conn):
    """Must converge — this is what bug 851005b was about."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await cleanup_reassigned_faces(conn) == 0
    assert await cleanup_reassigned_faces(conn) == 0


async def test_propagates_an_unassignment(conn):
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await _link(conn, src_asset, tgt_asset, src, tgt)
    await make_face(conn, src_asset, person_group_id=None, bbox=(1, 1, 9, 9))
    await make_face(conn, tgt_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await cleanup_reassigned_faces(conn) == 1
    now = await conn.fetchval(
        'SELECT "personGroupId" FROM asset_face WHERE "assetId" = $1', tgt_asset
    )
    assert now is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `make test PYTEST_ARGS=tests/test_cleanup.py`
Expected: FAIL — `column "personId" does not exist`.

- [ ] **Step 3: Drop the now-unused import**

In `src/cleanup.py`, delete line 6:

```python
from src.person_sync import get_or_create_target_person
```

- [ ] **Step 4: Replace cleanup_reassigned_faces**

Replace the whole `cleanup_reassigned_faces` function in `src/cleanup.py` with:

```python
async def cleanup_reassigned_faces(conn: asyncpg.Connection) -> int:
    """Propagate source-side face reassignment to the target copy.

    Under cluster groups the person group id is shared, so this is a straight
    copy — no mapping table, no canonical resolution, no mirror-of-mirror loop.
    Matching is by exact bounding box, which is how the face was copied in the
    first place.

    The UPDATE is idempotent: once the target matches the source the WHERE
    clause stops selecting it, so repeated cycles converge.
    """
    updated = await conn.fetch(
        """
        UPDATE asset_face tf
        SET "personGroupId" = sf."personGroupId"
        FROM _face_sync_asset_map m
        JOIN asset_face sf ON sf."assetId" = m.source_asset_id AND sf."deletedAt" IS NULL
        WHERE tf."assetId" = m.target_asset_id
          AND tf."deletedAt" IS NULL
          AND tf."boundingBoxX1" = sf."boundingBoxX1"
          AND tf."boundingBoxY1" = sf."boundingBoxY1"
          AND tf."boundingBoxX2" = sf."boundingBoxX2"
          AND tf."boundingBoxY2" = sf."boundingBoxY2"
          AND tf."personGroupId" IS DISTINCT FROM sf."personGroupId"
        RETURNING tf.id, tf."personGroupId"
        """,
    )

    if updated:
        logger.info("Reassigned %d target faces to match source", len(updated))

    return len(updated)
```

- [ ] **Step 5: Run the tests**

Run: `make test PYTEST_ARGS=tests/test_cleanup.py`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/cleanup.py tests/test_cleanup.py
git commit -m "refactor: propagate face reassignment via shared personGroupId

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 6: Drop the person mapping table and wire up the engine

**Files:**
- Modify: `src/asset_sync.py` (remove `_face_sync_person_map` DDL and any reference)
- Modify: `src/sync_engine.py:10` (imports), `:87-98` (Phase 3/4 calls)
- Modify: `src/main.py` (table creation, if the DDL lives there)
- Create: `tests/test_sync_engine.py`

**Interfaces:**
- Consumes: everything from Tasks 2-5.
- Produces: `run_full_sync() -> dict` with the `persons_cleaned` / `faces_reassigned` keys retained and `_face_sync_person_map` gone.

- [ ] **Step 1: Find every remaining reference**

```bash
grep -rn '_face_sync_person_map\|get_or_create_target_person\|"personId"\|person\.id' src/ test_sync.py
```

Expected after this task: zero hits. Record the current hits — each one must be removed in Step 2.

- [ ] **Step 2: Remove the mapping table**

Delete the `CREATE TABLE ... _face_sync_person_map` statement and its indexes from wherever the grep found them (`src/asset_sync.py` or `src/main.py`). Add a one-time drop next to the remaining table DDL so existing deployments clean up:

```python
    # Dropped in the v3.2.0 cluster-group port: person identity is now shared
    # via Immich's person_group table, so the sidecar no longer maps persons.
    await conn.execute("DROP TABLE IF EXISTS _face_sync_person_map")
```

- [ ] **Step 3: Fix the sync_engine imports**

In `src/sync_engine.py`, replace line 10:

```python
from src.person_sync import cleanup_orphaned_persons, sync_person_names, sync_person_thumbnails, sync_person_visibility
```

That import list is unchanged — all four functions still exist with the same names. Verify no import of `get_or_create_target_person` remains anywhere.

- [ ] **Step 4: Write an end-to-end test**

Create `tests/test_sync_engine.py`:

```python
from src.cleanup import cleanup_reassigned_faces
from src.ml_sync import sync_faces_for_asset
from src.person_sync import cleanup_orphaned_persons, sync_person_names
from tests.conftest import (
    make_cluster_group, make_asset, make_face, make_person, make_person_group, make_user,
)


async def test_full_face_flow_converges(conn):
    """Sync a face, rename at source, reassign at source — all must converge."""
    cg = await make_cluster_group(conn)
    src = await make_user(conn, cluster_group_id=cg)
    tgt = await make_user(conn, cluster_group_id=cg)
    pg = await make_person_group(conn, cg)
    await make_person(conn, src, pg, name="")

    src_asset = await make_asset(conn, src)
    tgt_asset = await make_asset(conn, tgt)
    await conn.execute(
        """
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, NOW())
        """,
        src_asset, tgt_asset, src, tgt,
    )
    await make_face(conn, src_asset, person_group_id=pg, bbox=(1, 1, 9, 9))

    assert await sync_faces_for_asset(conn, src_asset, tgt_asset, src, tgt) == 1

    # Source user names the person later
    await conn.execute(
        'UPDATE person SET name = $1 WHERE "ownerId" = $2 AND "personGroupId" = $3',
        "Jacinta", src, pg,
    )
    assert await sync_person_names(conn) == 1

    name = await conn.fetchval(
        'SELECT name FROM person WHERE "ownerId" = $1 AND "personGroupId" = $2', tgt, pg
    )
    assert name == "Jacinta"

    # Second cycle must be a no-op everywhere
    assert await sync_person_names(conn) == 0
    assert await cleanup_reassigned_faces(conn) == 0
    assert await cleanup_orphaned_persons(conn) == 0


async def test_person_map_table_is_gone(conn):
    exists = await conn.fetchval(
        "SELECT to_regclass('_face_sync_person_map')"
    )
    assert exists is None
```

- [ ] **Step 5: Run the full suite**

Run: `make test`
Expected: all tests pass (roughly 20). `test_person_map_table_is_gone` will fail if the scratch DB still has the table from an earlier run — rebuild it with `make testdb-clean && make testdb`.

- [ ] **Step 6: Commit**

```bash
git add src/asset_sync.py src/sync_engine.py src/main.py tests/test_sync_engine.py
git commit -m "refactor: drop _face_sync_person_map

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 7: Update documentation

**Files:**
- Modify: `README.md:38` (prerequisites), `README.md:7` (the problem statement), `README.md:399-402` (phase descriptions)
- Modify: `CLAUDE.md` (Immich Schema Constraints section)
- Modify: `docs/architecture.mmd`

- [ ] **Step 1: Update the README problem statement**

The original framing ("face recognition doesn't work on partner-shared photos") is no longer true in v3.2.0. Replace `README.md:7` with:

```markdown
Immich's built-in partner sharing lets you view another user's library, but it shares your entire library or nothing, and shared assets stay in the other user's account — they don't land in your own timeline, memories, or albums. Since v3.2.0 cluster groups do let you see your own people in shared assets, so if a shared album is enough for you, you may not need this sidecar at all. Use it when you want the other user to genuinely *own* a curated subset of the photos.
```

- [ ] **Step 2: Update the prerequisites**

Replace the version line at `README.md:38`:

```markdown
- **Immich v3.2.0** (tested). Earlier versions are not supported: v3.2.0 moved person identity into `person_group` and dropped `person.id`.
- **All configured users must share one Immich cluster group** (Account Settings > Sharing > Cluster group). The sidecar copies `asset_face."personGroupId"` verbatim, so source and target must resolve the same identities. **Joining a cluster group requires resetting facial recognition for every member, which discards existing names and birth dates.** The sidecar refuses to start otherwise.
```

- [ ] **Step 3: Update the phase description**

At `README.md:399-402`, Phase 3 and 4 descriptions should say the sidecar creates the target's `person` row on the shared group and fills empty names, rather than mirroring persons.

- [ ] **Step 4: Update CLAUDE.md schema constraints**

In the "Immich Schema Constraints" section, replace the `person.faceAssetId` bullet and add:

```markdown
- Since Immich v3.2.0: person identity lives in `person_group` (FK to `cluster_group`). `person.id` is **gone** — PK is `(ownerId, personGroupId)`. `asset_face."personId"` is now `"personGroupId"`, FK to `person_group`, `ON DELETE SET NULL`.
- `person.faceAssetId` is still an FK to `asset_face.id` — it must point at a face the *same user* owns.
- Person thumbnail: `/data/thumbs/{ownerId}/{personGroupId[0:2]}/{personGroupId[2:4]}/{personGroupId}.jpeg`
- Immich's `deleteEmptyGroups` drops any `person_group` with no `person` row and nulls its faces — always create the target's `person` row.
- `user.clusterGroupId` is a single NOT NULL uuid: a user is in exactly one cluster group.
```

Also update the "Tracking tables" section to remove `_face_sync_person_map`, and the "Key modules" description of `person_sync.py`.

- [ ] **Step 5: Update the architecture diagram**

Edit `docs/architecture.mmd` to remove the person-mirroring box and the `_face_sync_person_map` store. Regenerate:

```bash
npx -y @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.svg
npx -y @mermaid-js/mermaid-cli -i docs/architecture.mmd -o docs/architecture.png
```

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md docs/
git commit -m "docs: document v3.2.0 cluster-group requirement

Claude-Session: https://claude.ai/code/session_013Y1rrQ7vznT3DVPx6qa6Ch"
```

---

## Task 8: Live verification

Automated tests use synthetic rows. This confirms the real thing against a real Immich before you trust it with your library.

- [ ] **Step 1: Back up the live database**

```bash
docker exec immich_postgres pg_dump -U postgres immich > immich_backup_pre_clustergroup.sql
ls -lh immich_backup_pre_clustergroup.sql
```

Expected: a non-empty file. Do not proceed without it.

- [ ] **Step 2: Create the cluster group in Immich**

In Immich web, signed in as `doc.test@z9.io`: Account Settings > Sharing > Cluster group. Invite `doc.jacinta@z9.io` and `doc.tester@z9.io`. Each accepts. Then use the per-user reset button to re-run facial recognition for the group.

Expected: all three users show the same group. Confirm in SQL:

```bash
docker exec immich_postgres psql -U postgres immich -c \
  'SELECT id, email, "clusterGroupId" FROM "user";'
```

Expected: all three rows share one `clusterGroupId`. Before the join they are three distinct values (verified 2026-09-11). This step is what discards the 6 existing names.

- [ ] **Step 3: Wait for recognition to finish**

Watch the Immich job queue until Facial Recognition is empty. Do **not** use `force=true` on any job.

- [ ] **Step 4: Run one manual sync cycle**

Use the `test_sync.py` invocation documented in `CLAUDE.md`, with `LOG_LEVEL=DEBUG`.

Expected: no exceptions; stats show assets and faces synced.

- [ ] **Step 5: Verify the target sees shared identities**

```bash
docker exec immich_postgres psql -U postgres immich -c \
  'SELECT p."ownerId", p."personGroupId", p.name, COUNT(af.id) AS faces
   FROM person p
   LEFT JOIN asset_face af ON af."personGroupId" = p."personGroupId"
   GROUP BY 1,2,3 ORDER BY p."personGroupId" LIMIT 20;'
```

Expected: person groups with two rows each (one per owner), same `personGroupId`, and faces attached.

- [ ] **Step 6: Confirm Immich did not re-queue ML work**

Check the Immich job queues for Face Detection, Facial Recognition and Smart Search.

Expected: no new jobs queued for the synced target assets. If jobs appear, the `personGroupId` copy or `asset_job_status` pre-population is wrong — stop and diagnose before syncing more.

- [ ] **Step 7: Commit any fixes and tag**

```bash
git tag -a v3.2.0-port -m "Ported to Immich v3.2.0 cluster groups"
```

---

## Self-Review Notes

- **Coverage:** every v3.2.0 schema change in the Global Constraints table is handled — `person` PK (Tasks 2, 3), `asset_face.personGroupId` (Tasks 4, 5), thumbnail path (Task 3), new tables (Task 2), `user.clusterGroupId` (Task 2). `person_audit` needs no change: the sidecar never writes it, and its trigger fires from `person` deletes automatically.
- **Known gap:** `sync_person_names` only fills empty names, which is a behaviour change from today's unconditional overwrite. This is deliberate — names are per-user by design in v3.2.0 and the target user may have named someone themselves. If you want the old overwrite behaviour, drop the `(t.name = '' OR t.name IS NULL)` clause from the WHERE in Task 3 Step 3.
- **Not covered:** birth-date sync. The old code copied `birthDate` on person creation only, and this plan preserves that. Ongoing birth-date changes at the source still do not propagate, same as before.
