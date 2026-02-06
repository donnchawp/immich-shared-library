from dataclasses import dataclass
from functools import cached_property
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


@dataclass(frozen=True)
class SyncJob:
    name: str
    source_user_id: UUID
    target_user_id: UUID
    target_library_id: UUID
    source_path_prefix: str
    target_path_prefix: str


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

    # Album (optional)
    target_album_id: str = ""

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
        jobs = []
        if self.shared_path_prefix:
            jobs.append(SyncJob(
                name="external-library",
                source_user_id=self.source_uid,
                target_user_id=self.target_uid,
                target_library_id=self.target_lid,
                source_path_prefix=self.shared_path_prefix,
                target_path_prefix=self.target_path_prefix,
            ))
        if self.upload_source_user_id:
            jobs.append(SyncJob(
                name="internal-library",
                source_user_id=self.upload_source_uid,
                target_user_id=self.upload_target_uid,
                target_library_id=self.upload_target_lid,
                source_path_prefix=self.upload_path_prefix,
                target_path_prefix=self.target_upload_path_prefix,
            ))
        return jobs


settings = Settings()
