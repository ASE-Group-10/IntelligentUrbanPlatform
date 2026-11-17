"""Detect contradictory structured constraints among returned passages."""

import itertools
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from app.components.c09_logistics_knowledge.contracts import (
    ConflictDetails,
    Constraint,
    Passage,
    PolicyQuery,
)


def conflict_id(passage_ids: list[UUID], corpus_version: int) -> UUID:
    """Same passages in the same corpus version always give the same ID."""
    key = ",".join(sorted(map(str, passage_ids))) + f"@{corpus_version}"
    return uuid5(NAMESPACE_URL, f"c09-conflict:{key}")


def _applies(c: Constraint, query: PolicyQuery) -> bool:
    zones_ok = not c.zone_ids or bool(set(c.zone_ids) & set(query.area.zone_ids))
    vehicle_ok = not c.vehicle_classes or query.vehicle_class in c.vehicle_classes
    return zones_ok and vehicle_ok


def _days_overlap(a: Constraint, b: Constraint) -> bool:
    return (
        not a.days_of_week
        or not b.days_of_week
        or bool(set(a.days_of_week) & set(b.days_of_week))
    )


def _rule(c: Constraint) -> str:
    parts = []
    if c.time_of_day:
        parts.append(f"{c.time_of_day.from_}-{c.time_of_day.to}")
    if c.days_of_week:
        parts.append("/".join(c.days_of_week))
    if c.value is not None:
        parts.append(json.dumps(c.value, sort_keys=True))
    return " ".join(parts) or "no limits"


def detect_conflicts(
    passages: list[Passage], query: PolicyQuery, corpus_version: int
) -> list[ConflictDetails]:
    """One conflict per pair of passages from different documents whose
    constraints of the same kind both apply to the query but set different rules.

    shortcut: two different rules on overlapping days count as a conflict even
    when their time windows do not overlap; refine if curators report noise.
    """
    found = []
    for p, q in itertools.combinations(passages, 2):
        if p.document_id == q.document_id:
            continue
        for a, b in itertools.product(p.constraints, q.constraints):
            if (
                a.kind == b.kind
                and _applies(a, query)
                and _applies(b, query)
                and _days_overlap(a, b)
                and _rule(a) != _rule(b)
            ):
                ids = [p.passage_id, q.passage_id]
                found.append(
                    ConflictDetails(
                        conflict_id=conflict_id(ids, corpus_version),
                        passage_ids=ids,
                        document_ids=[p.document_id, q.document_id],
                        reason=(
                            f"Both apply to {query.vehicle_class} in "
                            f"{', '.join(query.area.zone_ids)} but the {a.kind} "
                            f"rules differ ({_rule(a)} vs {_rule(b)})."
                        ),
                    )
                )
                break
    return found
