import json
from pathlib import Path
from typing import Any

import pytest

COMPONENTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "components"


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
