import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from src.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def init_pool() -> asyncpg.Pool:
    """Return the connection pool, creating it if necessary.

    Idempotent and concurrency-safe: the sync and scan loops run concurrently,
    so a double-checked lock prevents two coroutines from racing to create two
    pools when the pool is absent (e.g. after a failed reset_pool()).
    """
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    host=settings.db_hostname,
                    port=settings.db_port,
                    user=settings.db_username,
                    password=settings.db_password.get_secret_value(),
                    database=settings.db_database_name,
                    min_size=2,
                    max_size=10,
                )
                logger.info("Database connection pool created")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed")


async def reset_pool() -> None:
    """Close and recreate the connection pool to recover from stale connections."""
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None
    await init_pool()
    logger.info("Database connection pool reset")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    # Lazily (re)create the pool so a failed reset_pool() can recover on the
    # next operation instead of wedging the loop with "pool not initialized".
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


async def fetch_all(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetch_one(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(query, *args)
