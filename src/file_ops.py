import logging
import os
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)


def hardlink_asset_files(
    source_user_id: UUID,
    target_user_id: UUID,
    source_asset_id: UUID,
    target_asset_id: UUID,
    source_files: list[dict],
) -> list[dict]:
    """Create hardlinks for thumbnail/preview files from source to target user directory.

    Returns list of dicts with {type, path, is_edited, is_progressive} for the new files.
    """
    result = []
    for f in source_files:
        source_path = Path(f["path"])
        if not source_path.exists():
            logger.warning("Source file does not exist: %s", source_path)
            continue

        # Replace source user/asset IDs in path with target IDs
        target_path = _remap_path(source_path, source_user_id, target_user_id, source_asset_id, target_asset_id)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            logger.debug("Target file already exists: %s", target_path)
        else:
            try:
                os.link(str(source_path), str(target_path))
                logger.debug("Hardlinked %s -> %s", source_path, target_path)
            except OSError as e:
                logger.error("Failed to hardlink %s -> %s: %s", source_path, target_path, e)
                continue

        result.append({
            "type": f["type"],
            "path": str(target_path),
            "is_edited": f.get("is_edited", False),
            "is_progressive": f.get("is_progressive", False),
        })

    return result


def remove_hardlinks(target_files: list[str]) -> None:
    """Remove hardlinked files for a target asset."""
    for path_str in target_files:
        path = Path(path_str)
        if path.exists():
            try:
                path.unlink()
                logger.debug("Removed hardlink: %s", path)
            except OSError as e:
                logger.error("Failed to remove hardlink %s: %s", path, e)


def _remap_path(
    source_path: Path,
    source_user_id: UUID,
    target_user_id: UUID,
    source_asset_id: UUID,
    target_asset_id: UUID,
) -> Path:
    """Remap a file path from source user/asset to target user/asset.

    Immich stores thumbs at: /usr/src/app/upload/thumbs/{userId}/{assetId}.ext
    Replaces path components that exactly match the UUIDs rather than using
    substring replacement, which could match at the wrong position.
    """
    parts = list(source_path.parts)
    src_uid = str(source_user_id)
    tgt_uid = str(target_user_id)
    src_aid = str(source_asset_id)
    tgt_aid = str(target_asset_id)

    for i, part in enumerate(parts):
        if part == src_uid:
            parts[i] = tgt_uid
        elif part.startswith(src_aid):
            # Asset ID is typically the filename prefix: {assetId}-thumbnail.webp
            parts[i] = part.replace(src_aid, tgt_aid, 1)

    return Path(*parts) if parts else source_path
