import logging
import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncJob:
    name: str
    source_user_id: UUID
    target_user_id: UUID
    target_library_id: UUID
    source_path_prefix: str
    target_path_prefix: str
    album_id: UUID | None = field(default=None)


def load_sync_jobs(config_path: str) -> list[SyncJob]:
    """Load sync jobs from a YAML config file.

    Validates required fields, UUID format, and unique job names.
    Raises ValueError on invalid config.
    """
    import yaml
    path = Path(config_path)
    data = yaml.safe_load(path.read_text())

    if not isinstance(data, dict) or "sync_jobs" not in data:
        raise ValueError(f"{config_path}: must contain a 'sync_jobs' key")

    raw_jobs = data["sync_jobs"]
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError(f"{config_path}: 'sync_jobs' must be a non-empty list")

    required_fields = [
        "name", "source_user_id", "target_user_id",
        "target_library_id", "source_path_prefix", "target_path_prefix",
    ]

    jobs: list[SyncJob] = []
    names: set[str] = set()

    for i, raw in enumerate(raw_jobs):
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path}: sync_jobs[{i}] must be a mapping")

        missing = [f for f in required_fields if not raw.get(f)]
        if missing:
            raise ValueError(
                f"{config_path}: sync_jobs[{i}] missing required fields: {', '.join(missing)}"
            )

        name = str(raw["name"])
        if name in names:
            raise ValueError(f"{config_path}: duplicate job name '{name}'")
        names.add(name)

        try:
            album_id = UUID(raw["album_id"]) if raw.get("album_id") else None
            jobs.append(SyncJob(
                name=name,
                source_user_id=UUID(raw["source_user_id"]),
                target_user_id=UUID(raw["target_user_id"]),
                target_library_id=UUID(raw["target_library_id"]),
                source_path_prefix=raw["source_path_prefix"],
                target_path_prefix=raw["target_path_prefix"],
                album_id=album_id,
            ))
        except ValueError as e:
            raise ValueError(f"{config_path}: sync_jobs[{i}] ({name}): {e}") from e

    return jobs


class Settings(BaseSettings):
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_username: str = "postgres"
    db_password: SecretStr = SecretStr("postgres")
    db_database_name: str = "immich"

    immich_api_url: str = "http://immich_server:2283"
    immich_api_key: SecretStr = SecretStr("")

    sync_interval_seconds: int = Field(default=60, ge=5)

    source_user_id: str = ""
    target_user_id: str = ""
    target_library_id: str = ""
    shared_path_prefix: str = ""
    target_path_prefix: str = ""

    upload_location_mount: str = "/usr/src/app/upload"

    # Internal library sync (optional, disabled when upload_source_user_id is empty)
    upload_source_user_id: str = ""
    upload_target_user_id: str = ""
    upload_target_library_id: str = ""
    target_upload_path_prefix: str = ""

    # Album (optional, legacy — use per-job album_id in config.yaml)
    target_album_id: str = ""

    # Path to YAML config file (overrides per-job env vars when file exists)
    config_file: str = "/app/config.yaml"

    log_level: str = "INFO"

    @cached_property
    def source_uid(self) -> UUID:
        return UUID(self.source_user_id)

    @cached_property
    def target_uid(self) -> UUID:
        return UUID(self.target_user_id)

    @cached_property
    def target_lid(self) -> UUID:
        return UUID(self.target_library_id)

    @cached_property
    def upload_source_uid(self) -> UUID:
        return UUID(self.upload_source_user_id)

    @cached_property
    def upload_target_uid(self) -> UUID:
        if self.upload_target_user_id:
            return UUID(self.upload_target_user_id)
        return self.target_uid

    @cached_property
    def upload_target_lid(self) -> UUID:
        return UUID(self.upload_target_library_id)

    @cached_property
    def upload_path_prefix(self) -> str:
        """Source path prefix for internal library assets: {upload_location_mount}/library/{upload_source_user_id}/"""
        return f"{self.upload_location_mount}/library/{self.upload_source_user_id}/"

    @cached_property
    def target_album_uid(self) -> UUID | None:
        return UUID(self.target_album_id) if self.target_album_id else None

    @cached_property
    def sync_jobs(self) -> list[SyncJob]:
        # Check for YAML config file (env var override or default path)
        config_path = os.environ.get("CONFIG_FILE", self.config_file)
        if Path(config_path).is_file():
            logger.info("Loading sync jobs from %s", config_path)
            return load_sync_jobs(config_path)

        # Fallback: build jobs from env vars (backward compat)
        logger.info("No config.yaml found, using environment variables")
        album_id = self.target_album_uid
        jobs = []
        if self.shared_path_prefix:
            jobs.append(SyncJob(
                name="external-library",
                source_user_id=self.source_uid,
                target_user_id=self.target_uid,
                target_library_id=self.target_lid,
                source_path_prefix=self.shared_path_prefix,
                target_path_prefix=self.target_path_prefix,
                album_id=album_id,
            ))
        if self.upload_source_user_id:
            jobs.append(SyncJob(
                name="internal-library",
                source_user_id=self.upload_source_uid,
                target_user_id=self.upload_target_uid,
                target_library_id=self.upload_target_lid,
                source_path_prefix=self.upload_path_prefix,
                target_path_prefix=self.target_upload_path_prefix,
                album_id=album_id,
            ))
        return jobs


settings = Settings()
