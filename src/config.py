from functools import cached_property
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


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


settings = Settings()
