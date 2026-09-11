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
        "INSERT INTO cluster_group (id) VALUES ($1) RETURNING id", uuid4()
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


async def make_synced_pair(
    conn,
    source_user_id: UUID,
    target_user_id: UUID,
    *,
    source_asset_id: UUID | None = None,
    target_asset_id: UUID | None = None,
    person_group_id: UUID | None = None,
    synced_at: str = "NOW()",
) -> tuple[UUID, UUID]:
    """Record a synced source/target asset pair, as Phase 1 would.

    This is what puts a user pair in scope for the cross-user sync and cleanup
    queries: they all reach the pair through _face_sync_asset_map.

    Pass ``person_group_id`` to also put a face in that group on the synced
    target asset, which is what brings the *group* into scope for the person
    metadata syncs. That second step is easy to forget, and forgetting it makes
    a test pass against a no-op, so it lives here rather than at each call site.

    ``synced_at`` takes a SQL expression so callers can backdate the watermark
    (e.g. "NOW() - INTERVAL '1 day'") to make source faces look newer.

    Pass ``source_asset_id`` / ``target_asset_id`` to map assets you made
    yourself, when the test needs control over them — a specific originalPath,
    or an id deliberately absent from ``asset`` to simulate a hard delete.

    Returns (source_asset_id, target_asset_id).
    """
    if source_asset_id is None:
        source_asset_id = await make_asset(conn, source_user_id)
    if target_asset_id is None:
        target_asset_id = await make_asset(conn, target_user_id)
    await conn.execute(
        f"""
        INSERT INTO _face_sync_asset_map
            (source_asset_id, target_asset_id, source_user_id, target_user_id, synced_at)
        VALUES ($1, $2, $3, $4, {synced_at})
        """,
        source_asset_id, target_asset_id, source_user_id, target_user_id,
    )
    if person_group_id is not None:
        await make_face(conn, target_asset_id, person_group_id=person_group_id)
    return source_asset_id, target_asset_id
