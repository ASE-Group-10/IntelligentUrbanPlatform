"""dbmate identifies migrations by number, not by the full filename."""

from collections import Counter
from pathlib import Path


def test_dbmate_migration_versions_are_unique():
    folder = Path(__file__).resolve().parents[2] / "database/migrations"
    versions = Counter(path.name.split("_", 1)[0] for path in folder.glob("*.sql"))
    assert not [v for v, count in versions.items() if count != 1], (
        "Duplicate migration versions prevent dbmate and Compose from starting"
    )
