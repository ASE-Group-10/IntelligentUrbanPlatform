from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore"
    )

    database_url: SecretStr
    mock_dependencies: bool = True
    availability_max_age_seconds: int = 86400


def get_settings() -> Settings:
    return Settings()
