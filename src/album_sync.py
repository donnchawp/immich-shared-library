import logging
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from src.config import settings

logger = logging.getLogger(__name__)


async def add_assets_to_album(conn: asyncpg.Connection, asset_ids: list[UUID]) -> int:
    """Add newly synced assets to the target album.

    Skips if target_album_id is not configured. Uses ON CONFLICT DO NOTHING
    for idempotency.
    Returns the number of assets added.
    """
    album_id = settings.target_album_uid
    if album_id is None or not asset_ids:
        return 0

    rows = await conn.fetch(
        """
        INSERT INTO album_asset ("albumId", "assetId")
        SELECT $1, unnest($2::uuid[])
        ON CONFLICT DO NOTHING
        RETURNING "assetId"
        """,
        album_id,
        asset_ids,
    )

    count = len(rows)

    if count > 0:
        await conn.execute(
            'UPDATE album SET "updatedAt" = $1 WHERE id = $2',
            datetime.now(timezone.utc),
            album_id,
        )
        logger.info("Added %d assets to album %s", count, album_id)

    return count


async def backfill_album(conn: asyncpg.Connection) -> int:
    """Add all previously synced assets that are missing from the target album.

    This handles the case where TARGET_ALBUM_ID is configured after assets
    have already been synced.
    Returns the number of assets added.
    """
    album_id = settings.target_album_uid
    if album_id is None:
        return 0

    rows = await conn.fetch(
        """
        INSERT INTO album_asset ("albumId", "assetId")
        SELECT $1, m.target_asset_id
        FROM _face_sync_asset_map m
        WHERE NOT EXISTS (
            SELECT 1 FROM album_asset aa
            WHERE aa."albumId" = $1 AND aa."assetId" = m.target_asset_id
        )
        RETURNING "assetId"
        """,
        album_id,
    )

    count = len(rows)

    if count > 0:
        await conn.execute(
            'UPDATE album SET "updatedAt" = $1 WHERE id = $2',
            datetime.now(timezone.utc),
            album_id,
        )
        logger.info("Backfilled %d assets into album %s", count, album_id)

    return count
