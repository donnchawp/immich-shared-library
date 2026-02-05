from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_username: str = "postgres"
    db_password: str = "postgres"
    db_database_name: str = "immich"

    immich_api_url: str = "http://immich_server:2283"
    immich_api_key: str = ""

    sync_interval_seconds: int = 60

    source_user_id: str = ""
    target_user_id: str = ""
    target_library_id: str = ""
    shared_path_prefix: str = ""
    target_path_prefix: str = ""

    upload_location_mount: str = "/usr/src/app/upload"

    log_level: str = "INFO"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_username}:{self.db_password}"
            f"@{self.db_hostname}:{self.db_port}/{self.db_database_name}"
        )


settings = Settings()
