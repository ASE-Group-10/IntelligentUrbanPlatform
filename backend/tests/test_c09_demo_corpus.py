"""The synthetic demo corpus publishes cleanly and answers the scenario queries."""

from datetime import UTC, datetime
from uuid import uuid4

from app.components.c09_logistics_knowledge.contracts import (
    PolicyQuery,
    RetrievalStatus,
)
from app.components.c09_logistics_knowledge.demo_corpus import DEMO_ZONES, load
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c09_logistics_knowledge.store import InMemoryKnowledgeStore
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Ok

PLANNER = AuthContext(
    principal_id="service:c05",
    principal_type="Service",
    roles=["Service:C05"],
    correlation_id=uuid4(),
)
NOW = datetime(2026, 11, 20, 6, 55, tzinfo=UTC)


def _service(include_conflict: bool = False) -> LogisticsKnowledge:
    store = InMemoryKnowledgeStore(set(DEMO_ZONES), info=None)
    service = LogisticsKnowledge(store, clock=lambda: NOW)
    results = load(service, include_conflict)
    assert all(isinstance(r, Ok) for r in results), results
    return service


def _query(vehicle: str, zone: str = "dublin-D2") -> PolicyQuery:
    return PolicyQuery.model_validate(
        {
            "area": {"zoneIds": [zone]},
            "timeWindow": {
                "start": "2026-11-20T07:30:00Z",
                "end": "2026-11-20T09:00:00Z",
            },
            "vehicleClass": vehicle,
            "activity": "Delivery",
            # unranked (no freeText): ask for every passage so none is cut at random
            "maxPassages": 20,
        }
    )


def _documents(result) -> set[str]:
    return {p.document_id for p in result.value.passages}


def test_hgv_delivery_in_d2_finds_cordon_loading_and_weight_rules():
    result = _service().query_knowledge(PLANNER, _query("truck"))
    assert result.value.status is RetrievalStatus.OK
    assert _documents(result) == {
        "dcc-hgv-cordon",
        "dcc-loading-bays",
        "dcc-bridge-weight",
    }


def test_cargo_bike_gets_bike_guidance_not_hgv_rules():
    result = _service().query_knowledge(PLANNER, _query("cargo_bike"))
    assert _documents(result) == {"dcc-cargo-bikes", "dcc-loading-bays"}


def test_zone_without_policies_is_no_applicable():
    result = _service().query_knowledge(PLANNER, _query("van", zone="dublin-D8"))
    assert result.value.status is RetrievalStatus.NO_APPLICABLE


def test_conflict_scenario_flags_the_amendment():
    result = _service(include_conflict=True).query_knowledge(PLANNER, _query("truck"))
    assert result.value.status is RetrievalStatus.CONFLICT
    [conflict] = result.value.conflicts
    assert "07:00-19:00" in conflict.reason and "07:00-10:30" in conflict.reason


def test_loading_the_corpus_twice_is_idempotent():
    service = _service()
    assert all(r.value.duplicate for r in load(service))
