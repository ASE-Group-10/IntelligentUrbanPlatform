"""The Postgres test setup only ever resets an explicitly designated test database."""

import pytest

from tests.database_guard import is_dedicated_test_database


@pytest.mark.parametrize("name", ["ulop_test", "c06_test", "ulop_ci_test"])
def test_names_ending_in_underscore_test_are_accepted(name):
    assert is_dedicated_test_database(name)


@pytest.mark.parametrize(
    "name",
    [
        "latest",
        "test",
        "ulop",
        "postgres",
        "ulop_testing",
        "testdb",
        "contest",
        "_test",
    ],
)
def test_other_names_are_refused(name):
    assert not is_dedicated_test_database(name)
