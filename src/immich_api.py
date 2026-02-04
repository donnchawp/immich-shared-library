import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class ImmichAPI:
    def __init__(self) -> None:
        self._base_url = settings.immich_api_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "x-api-key": settings.immich_api_key,
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_libraries(self) -> list[dict]:
        """Fetch all external libraries."""
        resp = await self._client.get(f"{self._base_url}/api/libraries")
        resp.raise_for_status()
        return resp.json()

    async def scan_library(self, library_id: str) -> bool:
        """Trigger a library scan. Returns True on success."""
        try:
            resp = await self._client.post(f"{self._base_url}/api/libraries/{library_id}/scan")
            resp.raise_for_status()
            logger.debug("Triggered scan for library %s", library_id)
            return True
        except httpx.HTTPError:
            logger.exception("Failed to trigger scan for library %s", library_id)
            return False

    async def health_check(self) -> bool:
        """Check if Immich server is reachable."""
        try:
            resp = await self._client.get(f"{self._base_url}/api/server/ping", timeout=10)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
