import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class ImmichAPI:
    def __init__(self) -> None:
        self._base_url = settings.immich_api_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "x-api-key": settings.immich_api_key.get_secret_value(),
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        """Check if Immich server is reachable."""
        try:
            resp = await self._client.get(f"{self._base_url}/api/server/ping", timeout=10)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
