"""Settings read from the environment (``app/config.py``)."""

from uuid import UUID

import pytest

from app.config import Settings

DEMO_RUN = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
OTHER_RUN = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    for name in ("SHARED_RUN_IDS", "C06_SHARED_RUN_IDS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)


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
