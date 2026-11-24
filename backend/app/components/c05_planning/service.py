"""Generate and read the single-delivery Dublin proposal from walking-skeleton hop 4."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import psycopg

from app.components.c05_planning.models import Plan, PlanScope, Route, SavedDelivery
from app.components.c05_planning.store import GenerationConflictError, PostgresPlanStore
from app.components.c08_allocation.models import (
    Capacity,
    CapacityUnit,
    ResourceKind,
    ResourceRequirement,
)
from app.components.c09_logistics_knowledge.contracts import (
    Area,
    EvidenceSnapshot,
    PolicyQuery,
    RetrievalStatus,
    RuleCoverage,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c10_decision_records.contracts import DecisionRecord
from app.components.c10_decision_records.service import DecisionRecords
from app.demo.feed import Scenario
from app.shared.c09_c10_common import Activity, AuthContext, VehicleClass
from app.shared.result import Failure, Ok, Result

log = logging.getLogger(__name__)
_CALLER = "service:c04"
_PLANNER = "service:c05"
_DEMO_ZONES = ("dublin-D1", "dublin-D2")
_DEMO_DURATION_SECONDS = 360
_DEMO_LIMITATION = "Fixed Dublin demo route and van requirement; no route optimisation."


class _DecisionRejectedError(Exception):
    """Abort the caller-owned transaction when C10 returns a failure result."""

    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.message)


def _check_policy(snapshot: EvidenceSnapshot, segment_key: str) -> Failure | None:
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


class PlanningService:
    """C04 calls generation with the delivery it saved in the authorised run."""

    def __init__(
        self,
        store: PostgresPlanStore,
        knowledge: LogisticsKnowledge,
        decisions: DecisionRecords,
        scenario: Scenario,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._knowledge = knowledge
        self._decisions = decisions
        self._scenario = scenario
        self._clock = clock

    def get_plan(self, run_scope: PlanScope, plan_id: UUID) -> Result[Plan]:
        """Read the current committed version inside the authorised partition."""
        try:
            row = self._store.get_plan(run_scope.scope, run_scope.run_id, plan_id)
        except psycopg.Error:
            return Failure("Unavailable", "C05.PLAN_STORE_UNAVAILABLE")
        if row is None:
            return Failure("NotFound", "C05.PLAN_NOT_FOUND")
        return Ok(Plan.model_validate(row, strict=False))

    def generate_plan(
        self,
        delivery: SavedDelivery,
        run_scope: PlanScope,
        correlation_id: UUID,
        key: str,
    ) -> Result[Plan]:
        """Save proposal and decision atomically, replaying the first committed plan."""
        if run_scope.scope != "simulation":
            return Failure("Invalid", "C05.DUBLIN_SCENARIO_REQUIRES_SIMULATION")
        if not key.strip() or len(key) > 200:
            return Failure("Invalid", "C05.INVALID_IDEMPOTENCY_KEY")

        request = delivery.model_dump(mode="json", by_alias=True)
        ctx = AuthContext(
            principal_id=_PLANNER,
            principal_type="Service",
            roles=["Service:C05"],
            correlation_id=correlation_id,
        )
        try:
            with self._store.transaction() as conn:
                previous = self._store.replay(
                    conn, run_scope.scope, run_scope.run_id, _CALLER, key, request
                )
                if previous is not None:
                    plan = Plan.model_validate(previous, strict=False)
                else:
                    departure = max(
                        self._scenario.start_time, delivery.time_window.start
                    )
                    proposal = self._propose(delivery, run_scope, ctx, departure)
                    if isinstance(proposal, Failure):
                        return proposal
                    plan, decision = proposal.value
                    recorded = self._decisions.record_decision(conn, ctx, decision)
                    if isinstance(recorded, Failure):
                        raise _DecisionRejectedError(recorded)
                    self._store.save(
                        conn,
                        plan.model_dump(mode="json", by_alias=True),
                        correlation_id,
                        departure,
                        _CALLER,
                        key,
                        request,
                    )
        except _DecisionRejectedError as exc:
            return exc.failure
        except GenerationConflictError:
            return Failure("Conflict", "C05.IDEMPOTENCY_MISMATCH")
        except psycopg.Error:
            return Failure("Unavailable", "C05.PLAN_STORE_UNAVAILABLE")
        log.info(
            "generatePlan: plan %s v%d awaiting confirmation",
            plan.plan_id,
            plan.version,
            extra={
                "component": "C05",
                "correlationId": str(correlation_id),
                "scope": run_scope.scope,
                "runId": str(run_scope.run_id),
            },
        )
        return Ok(plan)

    def _propose(
        self,
        delivery: SavedDelivery,
        run_scope: PlanScope,
        ctx: AuthContext,
        departure: datetime,
    ) -> Result[tuple[Plan, DecisionRecord]]:
        if delivery.status != "pending" or delivery.progress.status != "pending":
            return Failure("Conflict", "C05.DELIVERY_NOT_PENDING")
        if VehicleClass.VAN not in delivery.constraints.vehicle_kinds:
            return Failure("Infeasible", "C05.DEMO_VAN_NOT_PERMITTED")
        if (
            departure + timedelta(seconds=_DEMO_DURATION_SECONDS)
            > delivery.time_window.end
        ):
            return Failure("Infeasible", "C05.DEMO_ROUTE_OUTSIDE_DELIVERY_WINDOW")
        query = PolicyQuery(
            area=Area(zone_ids=list(_DEMO_ZONES)),
            time_window=delivery.time_window,
            vehicle_class=VehicleClass.VAN,
            activity=Activity.DELIVERY,
        )
        knowledge = self._knowledge.query_knowledge(ctx, query)
        if isinstance(knowledge, Failure):
            return knowledge
        snapshot = knowledge.value.evidence_snapshot
        segment_key = self._scenario.closure.segment_key
        rejected = _check_policy(snapshot, segment_key)
        if rejected is not None:
            return rejected

        route = Route(
            delivery_ids=[delivery.delivery_id],
            segment_keys=[segment_key],
            distance_metres=1200,
            estimated_duration_seconds=_DEMO_DURATION_SECONDS,
        )
        plan = Plan(
            plan_id=uuid4(),
            version=1,
            status="awaiting_confirmation",
            request_ids=[delivery.delivery_id],
            run_scope=run_scope,
            routes=[route],
            resource_needs=[
                ResourceRequirement(
                    delivery_id=delivery.delivery_id,
                    resource_kind=ResourceKind.VAN,
                    capacity=Capacity(
                        amount=delivery.cargo.weight_kg, unit=CapacityUnit.KG
                    ),
                )
            ],
            decision_id=uuid4(),
            input_versions={
                f"delivery:{delivery.delivery_id}": delivery.version,
                "corpus": snapshot.corpus_version,
            },
        )
        decision = DecisionRecord(
            decision_id=plan.decision_id,
            idempotency_key=f"c05:plan:{plan.plan_id}:v{plan.version}",
            decision_type="PlanGenerated",
            producer="C05",
            decided_by=_PLANNER,
            subject={"type": "Plan", "id": plan.plan_id, "version": plan.version},
            attempt_outcome="Selected",
            related_decision_id=None,
            input_versions={
                "deliveries": [
                    {"id": delivery.delivery_id, "version": delivery.version}
                ],
                "resources": [],
                "citySnapshotVersion": None,
                "corpusVersion": snapshot.corpus_version,
            },
            selected_option={
                "optionId": "dublin-demo-van",
                "summary": "Fixed Dublin demo route via Eden Quay",
                "details": {
                    "segmentKeys": route.segment_keys,
                    "plannedDepartureAt": departure.isoformat(),
                },
            },
            considered_alternatives=[],
            reasons=[
                {
                    "reasonId": "dublin-demo",
                    "text": "The saved delivery permits the van on the demo route.",
                    "evidenceRefIds": [
                        passage.passage_id for passage in snapshot.items
                    ],
                }
            ],
            evidence_snapshots=[snapshot],
            limitations=[_DEMO_LIMITATION, *snapshot.limitations],
            run_scope={"mode": "Simulation", "runId": run_scope.run_id},
            decided_at=self._clock(),
        )
        return Ok((plan, decision))
