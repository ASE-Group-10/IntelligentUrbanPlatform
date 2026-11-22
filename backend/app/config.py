from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, AnyUrl, SecretStr, UrlConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore"
    )

    database_url: SecretStr
    supabase_url: (
        Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]
        | None
    ) = None
    mock_dependencies: bool = True
    availability_max_age_seconds: int = 86400
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None


def get_settings() -> Settings:
    return Settings()
