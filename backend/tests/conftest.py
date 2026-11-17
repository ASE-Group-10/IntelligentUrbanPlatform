import json
from pathlib import Path
from typing import Any

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "contracts" / "examples"


def read_example(name: str) -> dict[str, Any]:
    """Return a contract example from ``contracts/examples`` as parsed JSON."""
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def example():
    return read_example
