"""Safety check for the Postgres test database (used by ``conftest.py``)."""

import re

_DEDICATED_TEST_DATABASE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*_test")


def is_dedicated_test_database(name: str) -> bool:
    """True only for names that end in ``_test``, such as ``ulop_test``.

    The test setup drops and rebuilds schemas, so a looser check (for example
    "contains test") could reset a real database such as ``latest``.
    """
    return _DEDICATED_TEST_DATABASE.fullmatch(name) is not None
