import pytest

from src.schema import SchemaValidationError, validate_cluster_group, validate_schema
from tests.conftest import make_cluster_group, make_user


async def test_validate_schema_passes_against_real_v3_2_0_schema(conn):
    await validate_schema(conn)  # must not raise


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


async def test_soft_deleted_user_is_treated_as_missing(conn):
    cg = await make_cluster_group(conn)
    alice = await make_user(conn, cluster_group_id=cg)
    ghost = await make_user(conn)  # different cluster group
    await conn.execute('UPDATE "user" SET "deletedAt" = NOW() WHERE id = $1', ghost)

    with pytest.raises(SchemaValidationError, match="not found"):
        await validate_cluster_group(conn, [alice, ghost])
