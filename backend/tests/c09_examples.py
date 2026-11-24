"""Read C09/C10 contract examples from the component openapi.yaml files."""

import copy
import re
from functools import cache
from pathlib import Path

import yaml

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "components"


@cache
def _all() -> dict:
    found = {}
    for folder in ("c09-logistics-knowledge", "c10-decision-records"):
        text = (CONTRACTS / folder / "openapi.yaml").read_text(encoding="utf-8")
        found.update(yaml.safe_load(text)["components"]["examples"])
    return found


def example(name: str) -> dict:
    """``example("query_knowledge.request.json")`` -> a fresh copy of that example
    (the old file name, mapped to its ``C09QueryKnowledgeRequest`` component name)."""
    camel = "".join(
        w.capitalize() for w in re.split(r"[._]", name.removesuffix(".json"))
    )
    for prefix in ("C09", "C10"):
        if prefix + camel in _all():
            return copy.deepcopy(_all()[prefix + camel]["value"])
    raise KeyError(name)
