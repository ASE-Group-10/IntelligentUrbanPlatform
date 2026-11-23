from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import AnyUrl, SecretStr, UrlConstraints
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
    # Simulation runs every signed-in user may read through C06 until C12 records
    # run owners. Default: the thin-slice demo run (fixtures/dublin-scenario.json).
    c06_shared_run_ids: frozenset[UUID] = frozenset(
        {UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")}
    )


def get_settings() -> Settings:
    return Settings()
