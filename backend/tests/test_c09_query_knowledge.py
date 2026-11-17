"""UC09 queryKnowledge acceptance examples (#14), against the in-memory store."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.components.c09_logistics_knowledge.contracts import (
    Passage,
    PolicyQuery,
    RetrievalStatus,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c09_logistics_knowledge.store import InMemoryKnowledgeStore
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure, Ok
from tests.c09_examples import example as _example

NOW = datetime(2026, 11, 20, 6, 55, tzinfo=UTC)


def _passages() -> list[Passage]:
    ok = _example("query_knowledge.ok.response.json")
    return [Passage.model_validate(p) for p in ok["passages"]]


def _ctx(*roles: str) -> AuthContext:
    return AuthContext(
        principal_id="service:c05",
        principal_type="Service",
        roles=list(roles or ["Service:C05"]),
        correlation_id=uuid4(),
    )


@pytest.fixture
def query() -> PolicyQuery:
    return PolicyQuery.model_validate(_example("query_knowledge.request.json"))


def _service(store: InMemoryKnowledgeStore) -> LogisticsKnowledge:
    return LogisticsKnowledge(store, clock=lambda: NOW)


def test_returns_applicable_passages_sorted_with_snapshot(query):
    store = InMemoryKnowledgeStore({"dublin-D1", "dublin-D2"}, _passages())
    result = _service(store).query_knowledge(_ctx(), query)
    assert isinstance(result, Ok)
    assert result.value.status is RetrievalStatus.OK
    assert [p.score for p in result.value.passages] == [0.87, 0.74]
    assert result.value.evidence_snapshot.items == result.value.passages


def test_no_applicable_passage_gives_no_applicable():
    store = InMemoryKnowledgeStore({"dublin-D2", "dublin-D8"}, _passages())
    data = _example("query_knowledge.request.json")
    data["area"]["zoneIds"] = ["dublin-D8"]
    query = PolicyQuery.model_validate(data)
    result = _service(store).query_knowledge(_ctx(), query)
    assert isinstance(result, Ok)
    assert result.value.status is RetrievalStatus.NO_APPLICABLE
    assert result.value.passages == []
    assert result.value.limitations


def test_weak_best_match_gives_low_confidence(query):
    weak = [p.model_copy(update={"score": 0.4}) for p in _passages()]
    store = InMemoryKnowledgeStore({"dublin-D2"}, weak)
    result = _service(store).query_knowledge(_ctx(), query)
    assert isinstance(result, Ok)
    assert result.value.status is RetrievalStatus.LOW_CONFIDENCE


def test_unknown_zone_is_invalid(query):
    store = InMemoryKnowledgeStore({"dublin-D1"}, _passages())
    result = _service(store).query_knowledge(_ctx(), query)
    assert result == Failure("Invalid", "C09.UNKNOWN_ZONE: ['dublin-D2']")


def test_caller_without_query_role_is_forbidden(query):
    store = InMemoryKnowledgeStore({"dublin-D2"}, _passages())
    result = _service(store).query_knowledge(_ctx("KnowledgeCurator"), query)
    assert result == Failure("Forbidden", "C09.FORBIDDEN")


def test_unreachable_store_is_unavailable(query):
    store = InMemoryKnowledgeStore({"dublin-D2"}, _passages(), available=False)
    result = _service(store).query_knowledge(_ctx(), query)
    assert isinstance(result, Failure)
    assert result.kind == "Unavailable"


def test_same_query_gives_same_order_on_ties(query):
    tied = [p.model_copy(update={"score": 0.8}) for p in _passages()]
    forward = InMemoryKnowledgeStore({"dublin-D2"}, tied)
    backward = InMemoryKnowledgeStore({"dublin-D2"}, list(reversed(tied)))
    orders = [
        [
            p.passage_id
            for p in _service(s).query_knowledge(_ctx(), query).value.passages
        ]
        for s in (forward, forward, backward)
    ]
    assert orders[0] == orders[1] == orders[2] == sorted(orders[0], key=str)


def test_invalid_time_window_is_rejected_by_the_contract():
    data = _example("query_knowledge.request.json")
    data["timeWindow"]["end"] = data["timeWindow"]["start"]
    with pytest.raises(ValueError, match="end must be after start"):
        PolicyQuery.model_validate(data)
