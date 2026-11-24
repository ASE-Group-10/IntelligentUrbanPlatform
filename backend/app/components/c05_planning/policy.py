"""Dublin van policy inputs shared by generation and replanning."""

from app.components.c09_logistics_knowledge.contracts import (
    Area,
    EvidenceSnapshot,
    PolicyQuery,
    RetrievalStatus,
    RuleCoverage,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.shared.c09_c10_common import Activity, AuthContext, VehicleClass
from app.shared.result import Failure, Ok, Result

from .models import SavedDelivery

_DEMO_ZONES = ("dublin-D1", "dublin-D2")


def query_route_policy(
    knowledge: LogisticsKnowledge, ctx: AuthContext, delivery: SavedDelivery
) -> Result[EvidenceSnapshot]:
    query = PolicyQuery(
        area=Area(zone_ids=list(_DEMO_ZONES)),
        time_window=delivery.time_window,
        vehicle_class=VehicleClass.VAN,
        activity=Activity.DELIVERY,
    )
    result = knowledge.query_knowledge(ctx, query)
    if isinstance(result, Failure):
        return result
    return Ok(result.value.evidence_snapshot)


def check_route_policy(snapshot: EvidenceSnapshot, segment_key: str) -> Failure | None:
    if (
        snapshot.rule_coverage is RuleCoverage.INCOMPLETE
        or snapshot.retrieval_status
        in (
            RetrievalStatus.CONFLICT,
            RetrievalStatus.LOW_CONFIDENCE,
        )
    ):
        return Failure(
            "Unavailable", "C05.DATA_UNAVAILABLE: unreliable policy evidence"
        )
    for passage in snapshot.items:
        for rule in passage.constraints:
            if (
                set(rule.zone_ids).intersection(_DEMO_ZONES)
                and (not rule.segment_keys or segment_key in rule.segment_keys)
                and (
                    not rule.vehicle_classes or VehicleClass.VAN in rule.vehicle_classes
                )
            ):
                # The labelled route has no gross vehicle weight, permits or detailed
                # stop schedule with which to prove compliance with a matching rule.
                return Failure(
                    "Unavailable",
                    f"C05.DATA_UNAVAILABLE: demo cannot resolve {rule.constraint_id}",
                )
    return None
