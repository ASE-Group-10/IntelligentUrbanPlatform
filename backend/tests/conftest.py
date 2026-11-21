import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.database_guard import is_dedicated_test_database

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENTS_DIR = REPO_ROOT / "contracts" / "components"
MIGRATIONS_DIR = REPO_ROOT / "database" / "migrations"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
"""Dedicated test database, e.g. postgres://ulop:ulop@localhost:5432/ulop_test.

Postgres-backed tests are skipped when it is not set. Its schema is rebuilt from
``database/migrations`` and its tables are emptied before each test, so the
database name must end in ``_test``; anything else is refused before any change.
"""

C06_TABLES = (
    "c06_city_monitoring.outbox",
    "c06_city_monitoring.observations",
    "c06_city_monitoring.segment_conditions",
    "c06_city_monitoring.city_state",
)


def read_example(name: str) -> dict[str, Any]:
    """Return a contract example as parsed JSON.

    Examples live in ``contracts/components/<component>/examples/``. The file
    name must be unique across components.
    """
    matches = sorted(COMPONENTS_DIR.glob(f"*/examples/{name}"))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"expected exactly one contract example named {name!r}, "
            f"found {len(matches)}"
        )
    return json.loads(matches[0].read_text(encoding="utf-8"))


@pytest.fixture
def example():
    return read_example


def _migrate_up_sql(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"--\s*migrate:up\s*\n(.*?)--\s*migrate:down", text, re.S)
    if match is None:
        raise ValueError(f"{path.name} has no migrate:up/migrate:down sections")
    return match.group(1)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Rebuild the test database from the migrations once per test session."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping Postgres-backed tests")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        name = conn.execute("select current_database()").fetchone()[0]
        if not is_dedicated_test_database(name):
            raise RuntimeError(
                f"refusing to reset {name!r}: TEST_DATABASE_URL must name a "
                "dedicated test database ending in '_test', such as ulop_test"
            )
        conn.execute("drop schema if exists c06_city_monitoring cascade")
        conn.execute("drop schema if exists c07_disruption_detection cascade")
        conn.execute("drop schema if exists c09_logistics_knowledge cascade")
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(_migrate_up_sql(migration))
    return TEST_DATABASE_URL


@pytest.fixture
def c06_database(test_database_url: str) -> str:
    """Empty C06 tables before each Postgres-backed test."""
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(f"truncate {', '.join(C06_TABLES)}")
    return test_database_url


@pytest.fixture(params=["memory", "postgres"])
def new_store(request) -> Callable[[], Any]:
    """Factory for an empty City State store; tests run once per implementation."""
    from app.components.c06_city_monitoring.postgres_store import (
        PostgresCityStateStore,
    )
    from app.components.c06_city_monitoring.store import InMemoryCityStateStore

    if request.param == "memory":
        return InMemoryCityStateStore
    url = request.getfixturevalue("c06_database")
    return lambda: PostgresCityStateStore(url)
