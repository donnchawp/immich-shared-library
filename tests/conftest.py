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
