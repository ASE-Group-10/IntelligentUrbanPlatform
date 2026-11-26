"""Local provisioning cannot be silently redirected to a shared database."""

from contextlib import contextmanager
from urllib.parse import urlparse

import pytest

from app.components.c12_simulation_replay import local_setup


def test_setup_requires_explicit_local_compose_context(monkeypatch):
    monkeypatch.delenv("C12_LOCAL_COMPOSE", raising=False)

    def unexpected(*args, **kwargs):
        pytest.fail("Setup attempted a connection without its local-only guard")

    monkeypatch.setattr(local_setup.psycopg, "connect", unexpected)
    with pytest.raises(ValueError, match="local Compose"):
        local_setup.main()


def test_shared_database_environment_cannot_retarget_local_setup(monkeypatch):
    monkeypatch.setenv("C12_LOCAL_COMPOSE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgres://invalid/shared")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgres://invalid/shared")
    observed = []
    sentinel = object()

    @contextmanager
    def connect(conninfo, **kwargs):
        target = urlparse(conninfo)
        assert target.hostname == "127.0.0.1" and target.path == "/ulop"
        observed.append(target.hostname)
        yield sentinel

    def provision(conn):
        assert conn is sentinel
        observed.append("provisioned")

    monkeypatch.setattr(local_setup.psycopg, "connect", connect)
    monkeypatch.setattr(local_setup, "provision", provision)
    local_setup.main()
    assert observed == ["127.0.0.1", "provisioned"]
