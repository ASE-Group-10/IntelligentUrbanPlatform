"""Settings read from the environment and HTTP ingress middleware."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import DEFAULT_ALLOWED_HOSTS, HttpIngressSettings, Settings
from app.main import create_app

DEMO_RUN = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
OTHER_RUN = UUID("00000000-0000-4000-8000-000000000001")
PUBLIC_API_HOST = "ulop-dev-api.swedencentral.cloudapp.azure.com"
FRONTEND_ORIGIN = "https://ulop-dev.azurestaticapps.net"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    for name in (
        "SHARED_RUN_IDS",
        "C06_SHARED_RUN_IDS",
        "ALLOWED_HOSTS",
        "CORS_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)


def _sqlite_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def test_shared_runs_default_to_the_demo_run(env):
    assert settings().shared_run_ids == {DEMO_RUN}


def test_shared_runs_come_from_shared_run_ids(env):
    env.setenv("SHARED_RUN_IDS", f'["{OTHER_RUN}"]')

    assert settings().shared_run_ids == {OTHER_RUN}


def test_an_explicitly_empty_legacy_list_shares_no_runs(env):
    env.setenv("C06_SHARED_RUN_IDS", "[]")

    assert settings().shared_run_ids == frozenset()


def test_the_new_setting_wins_over_the_legacy_one(env):
    env.setenv("C06_SHARED_RUN_IDS", "[]")
    env.setenv("SHARED_RUN_IDS", f'["{OTHER_RUN}"]')

    assert settings().shared_run_ids == {OTHER_RUN}


def test_ingress_defaults_keep_local_hosts_and_disable_cors(env):
    cfg = settings()

    assert cfg.allowed_hosts == DEFAULT_ALLOWED_HOSTS
    assert cfg.cors_origins == ()


def test_ingress_settings_load_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ALLOWED_HOSTS", f"localhost, 127.0.0.1, {PUBLIC_API_HOST}")
    monkeypatch.setenv("CORS_ORIGINS", f"{FRONTEND_ORIGIN}/, http://localhost:5173")

    ingress = HttpIngressSettings(_env_file=None)

    assert ingress.allowed_hosts == ("localhost", "127.0.0.1", PUBLIC_API_HOST)
    assert ingress.cors_origins == (FRONTEND_ORIGIN, "http://localhost:5173")


@pytest.mark.parametrize(
    "bad_hosts",
    [
        "",
        "*",
        f"https://{PUBLIC_API_HOST}",
        f"{PUBLIC_API_HOST}:443",
        "example.com/api",
    ],
)
def test_allowed_hosts_rejects_wildcards_schemes_ports_and_paths(env, bad_hosts):
    env.setenv("ALLOWED_HOSTS", bad_hosts)

    with pytest.raises(ValidationError):
        settings()


@pytest.mark.parametrize(
    "bad_origins",
    ["*", PUBLIC_API_HOST, f"https://{PUBLIC_API_HOST}/api", "ftp://example.com"],
)
def test_cors_origins_rejects_wildcards_and_non_origin_urls(env, bad_origins):
    env.setenv("CORS_ORIGINS", bad_origins)

    with pytest.raises(ValidationError):
        settings()


def test_app_enforces_configured_allowed_hosts_and_cors_headers():
    engine = _sqlite_engine()
    app = create_app(
        engine=engine,
        settings=SimpleNamespace(
            database_url=SecretStr("sqlite://"),
            mock_dependencies=True,
            supabase_url=None,
            availability_max_age_seconds=86400,
            allowed_hosts=("localhost", PUBLIC_API_HOST),
            cors_origins=(FRONTEND_ORIGIN,),
        ),
    )

    with TestClient(app, base_url=f"https://{PUBLIC_API_HOST}") as client:
        preflight = client.options(
            "/api/v1/resources",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "Authorization, Content-Type, Idempotency-Key, "
                    "X-Acting-Provider, X-Correlation-ID"
                ),
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
        allowed = {
            h.strip().lower()
            for h in preflight.headers["access-control-allow-headers"].split(",")
        }
        assert {
            "authorization",
            "content-type",
            "idempotency-key",
            "x-acting-provider",
            "x-correlation-id",
        }.issubset(allowed)

        health = client.get("/health", headers={"Origin": FRONTEND_ORIGIN})
        assert health.status_code == 200
        assert health.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
        exposed = {
            h.strip().lower()
            for h in health.headers["access-control-expose-headers"].split(",")
        }
        assert {
            "idempotency-replayed",
            "location",
            "x-correlation-id",
            "x-plan-id",
            "x-plan-stub",
            "x-plan-version",
        }.issubset(exposed)

        unlisted_origin = client.get(
            "/health", headers={"Origin": "https://evil.example"}
        )
        assert unlisted_origin.status_code == 200
        assert "access-control-allow-origin" not in unlisted_origin.headers

    with TestClient(app, base_url="https://spoofed.example") as bad_host_client:
        rejected = bad_host_client.get("/health")
        assert rejected.status_code == 400
