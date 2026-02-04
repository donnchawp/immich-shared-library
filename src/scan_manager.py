import logging

from src.config import settings
from src.immich_api import ImmichAPI

logger = logging.getLogger(__name__)


class ScanManager:
    """Manages selective library scanning.

    Scans all libraries EXCEPT the target library (User B's shared library),
    replacing Immich's disabled global auto-scan with per-library control.
    """

    def __init__(self, api: ImmichAPI) -> None:
        self._api = api

    async def scan_all_except_target(self) -> int:
        """Trigger scans on all libraries except the target library.

        Returns the number of libraries scanned.
        """
        libraries = await self._api.get_libraries()
        count = 0

        for lib in libraries:
            lib_id = lib.get("id", "")
            if lib_id == settings.target_library_id:
                logger.debug("Skipping target library %s", lib_id)
                continue

            success = await self._api.scan_library(lib_id)
            if success:
                count += 1

        if count > 0:
            logger.info("Triggered scan on %d libraries", count)

        return count
