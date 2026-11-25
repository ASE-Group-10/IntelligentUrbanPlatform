import json
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    AliasChoices,
    AnyUrl,
    BeforeValidator,
    Field,
    SecretStr,
    UrlConstraints,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")


def _parse_string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ()
        if raw.startswith("["):
            parsed = json.loads(raw)
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError("Expected a JSON array of strings")
            items = parsed
        else:
            items = raw.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("Expected string entries")
        items = list(value)
    else:
        raise ValueError("Expected a comma-separated string or list of strings")

    return tuple(item.strip() for item in items if item.strip())


def _validate_allowed_hosts(hosts: tuple[str, ...]) -> tuple[str, ...]:
    if not hosts:
        raise ValueError("ALLOWED_HOSTS must include at least one hostname")
    for host in hosts:
        if host == "*" or "*" in host:
            raise ValueError("ALLOWED_HOSTS must list explicit hostnames, not '*'")
        has_whitespace = any(c.isspace() for c in host)
        if "://" in host or "/" in host or ":" in host or has_whitespace:
            raise ValueError(
                f"Invalid ALLOWED_HOSTS entry {host!r}; "
                "supply hostnames without scheme, port, or path"
            )
    return hosts


def _validate_cors_origins(origins: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for origin in origins:
        if origin == "*" or "*" in origin:
            raise ValueError("CORS_ORIGINS must list explicit http(s) origins, not '*'")
        parts = urlsplit(origin)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ValueError(
                f"Invalid CORS_ORIGINS entry {origin!r}; "
                "supply origins such as https://app.example.com"
            )
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError(f"Invalid CORS_ORIGINS port in {origin!r}") from exc
        hostname = parts.hostname
        if hostname is None:
            raise ValueError(f"Invalid CORS_ORIGINS hostname in {origin!r}")
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parts.scheme == "https" else 80
        suffix = f":{port}" if port is not None and port != default_port else ""
        normalized.append(f"{parts.scheme}://{host}{suffix}")
    return tuple(dict.fromkeys(normalized))


AllowedHosts = Annotated[
    tuple[str, ...],
    NoDecode,
    BeforeValidator(_parse_string_list),
    AfterValidator(_validate_allowed_hosts),
]

CorsOrigins = Annotated[
    tuple[str, ...],
    NoDecode,
    BeforeValidator(_parse_string_list),
    AfterValidator(_validate_cors_origins),
]


class HttpIngressSettings(BaseSettings):
    """Host and CORS allowlists needed when constructing FastAPI middleware."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    allowed_hosts: AllowedHosts = DEFAULT_ALLOWED_HOSTS
    cors_origins: CorsOrigins = ()


class Settings(HttpIngressSettings):
    database_url: SecretStr
    supabase_url: (
        Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]
        | None
    ) = None
    # Server-only Supabase key (service_role JWT or sb_secret_...) for C10 evidence
    # objects in Supabase Storage (#130). Unset means the storage path is off.
    supabase_service_key: SecretStr | None = None
    c10_evidence_bucket: str = "c10-evidence"
    mock_dependencies: bool = True
    availability_max_age_seconds: int = 86400
    # Simulation runs every signed-in user may read through C06 and C07 until C12
    # records run owners. Default: the thin-slice demo run
    # (fixtures/dublin-scenario.json). See app/shared/run_access.py.
    # SHARED_RUN_IDS wins; C06_SHARED_RUN_IDS (its name before #105) is still read
    # when it is the only one set, so existing configs keep their meaning.
    shared_run_ids: frozenset[UUID] = Field(
        default=frozenset({UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")}),
        validation_alias=AliasChoices("SHARED_RUN_IDS", "C06_SHARED_RUN_IDS"),
    )


def get_settings() -> Settings:
    return Settings()


def get_ingress_settings(settings=None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if settings is not None:
        raw_hosts = getattr(settings, "allowed_hosts", DEFAULT_ALLOWED_HOSTS)
        raw_origins = getattr(settings, "cors_origins", ())
        return (
            _validate_allowed_hosts(_parse_string_list(raw_hosts)),
            _validate_cors_origins(_parse_string_list(raw_origins)),
        )
    ingress = HttpIngressSettings()
    return ingress.allowed_hosts, ingress.cors_origins
