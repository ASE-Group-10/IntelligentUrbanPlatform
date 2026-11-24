"""C07's synchronous requestReplan for the fixed Dublin closure and detour."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.components.c06_city_monitoring.contracts import (
    CitySnapshotQuery,
    SnapshotScope,
)
from app.components.c06_city_monitoring.ingestion import CityMonitoring
from app.components.c06_city_monitoring.postgres_store import hold_snapshot_version
from app.components.c06_city_monitoring.store import StateScope
from app.components.c07_disruption_detection.contracts import (
    ReplanOutcome,
    ReplanRequest,
)
from app.components.c08_allocation.models import (
    Capacity,
    CapacityUnit,
    ResourceKind,
    ResourceRequirement,
)
from app.components.c09_logistics_knowledge.contracts import EvidenceSnapshot
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c10_decision_records.contracts import DecisionRecord
from app.components.c10_decision_records.service import DecisionRecords
from app.demo.feed import Scenario
from app.shared.c09_c10_common import AuthContext, VehicleClass
from app.shared.result import Failure, Ok, Result

from .facts import is_unfinished, linked_deliveries
from .models import Plan, PlanScope, Route, SavedDelivery
from .policy import check_route_policy, query_route_policy
from .store import DecisionRejectedError, PostgresPlanStore, lock_plan, scope_key
from .versions import save_ready_version

log = logging.getLogger(__name__)
# The UC07 revised-plan example defines the length and duration of the labelled detour.
_DETOUR_METRES = 1600
_DETOUR_SECONDS = 480


@dataclass(frozen=True)
class _Assessment:
    outcome: Literal["NoChange", "ReviewRequired", "Selected"]
    reason: str
    city_version: int | None = None
    evidence: EvidenceSnapshot | None = None
    route: Route | None = None
    departure: datetime | None = None


def _assess(
    plan: Plan,
    work: list[SavedDelivery],
    departure: datetime,
    request: ReplanRequest,
    ctx: AuthContext,
    city: CityMonitoring,
    knowledge: LogisticsKnowledge,
    scenario: Scenario,
) -> Result[_Assessment]:
    if not work:
        return Ok(
            _Assessment("NoChange", "No unfinished delivery remains on the plan.")
        )
    if request.trigger.reason != "road_closed":
        return Ok(
            _Assessment("ReviewRequired", "The Dublin slice handles road closures.")
        )
    detours = [
        s.segment_key for s in scenario.segments if s.role == "detour after the closure"
    ]
    if len(detours) != 1:
        return Failure("Invalid", "C05.DUBLIN_DETOUR_UNDEFINED")
    detour = detours[0]
    unfinished = {d.delivery_id for d in work}
    route_keys = {
        key
        for route in plan.routes
        if unfinished.intersection(route.delivery_ids)
        for key in route.segment_keys
    }
    read = city.get_city_snapshot(
        StateScope(scope=request.scope, run_id=request.run_id),
        CitySnapshotQuery(
            scope=SnapshotScope(segment_keys=sorted(route_keys | {detour}))
        ),
    )
    if isinstance(read, Failure):
        return read
    snapshot = read.value
    version = snapshot.snapshot_version
    if version < (request.trigger.city_snapshot_version or 0):
        return Failure("Unavailable", "C05.CITY_SNAPSHOT_BEHIND_TRIGGER")
    by_key = {s.segment_key: s for s in snapshot.segments}
    departure = max(departure, snapshot.generated_at)
    if not route_keys or any(
        s.quality != "good"
        or s.conflict
        or s.observed_at > snapshot.generated_at
        or (s.valid_until is not None and s.valid_until <= departure)
        for s in snapshot.segments
        if s.segment_key in route_keys
    ):
        return Ok(
            _Assessment(
                "ReviewRequired", "Current route conditions are uncertain.", version
            )
        )
    # C06 defines an absent segment as open. Reopening between detection and
    # this read must not divert a delivery on the basis of an old trigger.
    closed = [
        s
        for s in snapshot.segments
        if s.segment_key in route_keys and s.status == "closed"
    ]
    if not closed:
        return Ok(
            _Assessment(
                "NoChange",
                "The latest City State has no closed segment on the current route.",
                version,
            )
        )
    if (
        len(work) != 1
        or len(plan.request_ids) != 1
        or len(plan.routes) != 1
        or plan.routes[0].segment_keys != [scenario.closure.segment_key]
    ):
        return Ok(
            _Assessment(
                "ReviewRequired", "No fixed Dublin detour covers this plan.", version
            )
        )
    delivery = work[0]
    departure = max(departure, delivery.time_window.start)
    if closed[0].valid_until is not None and closed[0].valid_until <= departure:
        return Ok(
            _Assessment(
                "ReviewRequired",
                "The closure is unverified at the planned departure.",
                version,
            )
        )
    arrival = departure + timedelta(seconds=_DETOUR_SECONDS)
    alternative = by_key.get(detour)
    if alternative and (
        alternative.status != "open"
        or alternative.quality != "good"
        or alternative.conflict
        or alternative.observed_at > snapshot.generated_at
        or (alternative.valid_until is not None and alternative.valid_until < arrival)
    ):
        return Ok(
            _Assessment(
                "ReviewRequired",
                "Abbey Street Lower is unavailable or uncertain.",
                version,
            )
        )
    if alternative and (
        (alternative.delay_seconds or 0) > 0
        or (
            alternative.speed_kmh is not None
            and alternative.speed_kmh < _DETOUR_METRES * 3.6 / _DETOUR_SECONDS
        )
    ):
        return Ok(
            _Assessment(
                "ReviewRequired",
                "Reported traffic cannot support the fixed eight-minute Dublin detour.",
                version,
            )
        )
    if (
        delivery.progress.status == "failed"
        or VehicleClass.VAN not in delivery.constraints.vehicle_kinds
    ):
        return Ok(
            _Assessment(
                "ReviewRequired",
                "The current delivery cannot use the Dublin van detour.",
                version,
            )
        )
    if arrival > delivery.time_window.end:
        return Ok(
            _Assessment(
                "ReviewRequired",
                "The Dublin detour exceeds the delivery window.",
                version,
            )
        )
    retrieved = query_route_policy(knowledge, ctx, delivery)
    if isinstance(retrieved, Failure):
        return retrieved
    evidence = retrieved.value
    rejected = check_route_policy(evidence, detour)
    if rejected:
        return Ok(_Assessment("ReviewRequired", rejected.message, version, evidence))
    route = Route(
        delivery_ids=[delivery.delivery_id],
        segment_keys=[detour],
        distance_metres=_DETOUR_METRES,
        estimated_duration_seconds=_DETOUR_SECONDS,
    )
    return Ok(
        _Assessment(
            "Selected",
            "Eden Quay is closed. The Abbey Street Lower detour fits the delivery.",
            version,
            evidence,
            route,
            departure,
        )
    )


def _record(
    assessment: _Assessment,
    plan: Plan,
    deliveries: list[SavedDelivery],
    request: ReplanRequest,
    now: datetime,
) -> DecisionRecord:
    evidence, route = assessment.evidence, assessment.route
    selected = None
    alternatives = []
    if route and assessment.departure:
        selected = {
            "optionId": "dublin-demo-detour",
            "summary": "Fixed detour via Abbey Street Lower",
            "details": {
                "segmentKeys": route.segment_keys,
                "plannedDepartureAt": assessment.departure.isoformat(),
                "trigger": request.trigger.model_dump(mode="json", by_alias=True),
            },
        }
        alternatives.append(
            {
                "optionId": "keep-current-route",
                "summary": "Keep the route via Eden Quay",
                "rejectedReason": "The current C06 snapshot reports a closure.",
                "evidenceRefIds": [],
            }
        )
    return DecisionRecord(
        decision_id=uuid4(),
        idempotency_key=f"c05:replan:{request.run_id}:{request.key}",
        decision_type="Replan",
        producer="C05",
        decided_by="service:c05",
        subject={
            "type": "Plan",
            "id": plan.plan_id,
            "version": plan.version + 1 if selected else None,
        },
        attempt_outcome=assessment.outcome,
        related_decision_id=plan.decision_id,
        input_versions={
            "deliveries": [
                {"id": d.delivery_id, "version": d.version} for d in deliveries
            ],
            "resources": [],
            "citySnapshotVersion": assessment.city_version,
            "corpusVersion": evidence.corpus_version if evidence else None,
        },
        selected_option=selected,
        considered_alternatives=alternatives,
        reasons=[
            {
                "reasonId": "dublin-replan",
                "text": assessment.reason,
                "evidenceRefIds": [p.passage_id for p in evidence.items]
                if evidence
                else [],
            }
        ],
        evidence_snapshots=[evidence] if evidence else [],
        limitations=[
            "One fixed van detour; C08 owns vehicle commitments.",
            *(evidence.limitations if evidence else []),
        ],
        run_scope={"mode": "Simulation", "runId": request.run_id},
        decided_at=now,
    )


@dataclass(frozen=True)
class _Receipt:
    outcome: ReplanOutcome
    event_id: UUID | None
    correlation_id: UUID
    source_event_id: UUID
    replayed: bool = False


def _apply(
    conn: psycopg.Connection,
    request: ReplanRequest,
    correlation_id: UUID,
    knowledge: LogisticsKnowledge,
    decisions: DecisionRecords,
    city: CityMonitoring,
    scenario: Scenario,
    clock: Callable[[], datetime],
) -> Result[_Receipt]:
    scope = PlanScope(scope=request.scope, run_id=request.run_id)
    scoped = scope_key(scope.scope, scope.run_id)
    trigger = request.trigger
    identity = (
        scoped,
        request.plan_id,
        trigger.disruption_id,
        trigger.disruption_version,
    )
    base = trigger.based_on_plan_version
    trigger_body = trigger.model_dump(mode="json", by_alias=True)
    lock_plan(conn, scope, request.plan_id)
    prior = conn.execute(
        """select base_version, trigger, outcome, event_id, correlation_id
             from c05_planning.replan_receipts
            where scope_key=%s and plan_id=%s
              and disruption_id=%s and disruption_version=%s
              and (base_version=%s or (base_version < %s and event_id is not null))
            order by base_version desc limit 1""",
        (*identity, base, base),
    ).fetchone()
    if prior:
        saved_base, saved_trigger, outcome, event_id, trace_id = prior
        if base == saved_base and trigger_body != saved_trigger:
            return Failure("Invalid", "C05.IDEMPOTENCY_MISMATCH")
        return Ok(
            _Receipt(
                ReplanOutcome.model_validate(outcome),
                event_id,
                trace_id,
                UUID(saved_trigger["sourceEventId"]),
                replayed=True,
            )
        )
    row = conn.execute(
        """select proposal, planned_departure_at from c05_planning.plan_versions
            where scope_key=%s and plan_id=%s order by version desc limit 1""",
        (scoped, request.plan_id),
    ).fetchone()
    if row is None:
        return Failure("NotFound", "C05.PLAN_NOT_FOUND")
    plan = Plan.model_validate(row[0], strict=False)
    if plan.status != "confirmed":
        return Failure("Invalid", "C05.PLAN_NOT_CONFIRMED")
    if plan.version != base:
        return Failure("Conflict", "C05.PLAN_VERSION_CHANGED")
    facts = linked_deliveries(conn, plan, correlation_id)
    if isinstance(facts, Failure):
        return facts
    work = [d for d in facts.value if is_unfinished(d)]
    ctx = AuthContext(
        principal_id="service:c05",
        principal_type="Service",
        roles=["Service:C05"],
        correlation_id=correlation_id,
    )
    checked = _assess(plan, work, row[1], request, ctx, city, knowledge, scenario)
    if isinstance(checked, Failure):
        return checked
    assessment = checked.value
    if assessment.city_version is not None:
        held = hold_snapshot_version(
            conn, StateScope(request.scope, request.run_id), assessment.city_version
        )
        if isinstance(held, Failure):
            return held
    now = clock()
    decision = _record(assessment, plan, facts.value, request, now)
    recorded = decisions.record_decision(conn, ctx, decision)
    if isinstance(recorded, Failure):
        # C10 may already have written on this connection. Roll the caller back.
        raise DecisionRejectedError(recorded)
    event_id = None
    if assessment.outcome == "Selected":
        assert (
            assessment.route
            and assessment.departure
            and assessment.evidence
            and assessment.city_version
        )
        revised = plan.model_copy(
            update={
                "version": base + 1,
                "decision_id": decision.decision_id,
                "routes": [assessment.route],
                "resource_needs": [
                    ResourceRequirement(
                        delivery_id=d.delivery_id,
                        resource_kind=ResourceKind.VAN,
                        capacity=Capacity(
                            amount=d.cargo.weight_kg, unit=CapacityUnit.KG
                        ),
                    )
                    for d in work
                ],
                "input_versions": {
                    **{f"delivery:{d.delivery_id}": d.version for d in facts.value},
                    "cityState": assessment.city_version,
                    "corpus": assessment.evidence.corpus_version,
                },
            }
        )
        event_id = save_ready_version(
            conn,
            revised,
            assessment.departure,
            correlation_id,
            now,
            causation_id=trigger.source_event_id,
        )
        outcome = ReplanOutcome(
            status="PlanSaved",
            new_plan_version=revised.version,
            decision_id=decision.decision_id,
        )
    else:
        outcome = ReplanOutcome(status=assessment.outcome, reason=assessment.reason)
    conn.execute(
        """insert into c05_planning.replan_receipts
             (scope_key, plan_id, disruption_id, disruption_version, base_version,
              trigger, correlation_id, outcome, event_id)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            *identity,
            base,
            Jsonb(trigger_body),
            correlation_id,
            Jsonb(outcome.model_dump(mode="json", by_alias=True, exclude_none=True)),
            event_id,
        ),
    )
    return Ok(_Receipt(outcome, event_id, correlation_id, trigger.source_event_id))


def request_replan(
    store: PostgresPlanStore,
    knowledge: LogisticsKnowledge,
    decisions: DecisionRecords,
    scenario: Scenario,
    clock: Callable[[], datetime],
    city: CityMonitoring,
    request: ReplanRequest,
    correlation_id: UUID,
) -> Result[ReplanOutcome]:
    """Return PlanSaved after the version, decision, receipt and outbox commit."""
    if request.scope != "simulation":
        return Failure("NotFound", "C05.PLAN_NOT_FOUND")
    if correlation_id.version != 4:
        return Failure("Invalid", "C05.INVALID_DISRUPTION_TRACE")
    try:
        with store.transaction() as conn:
            result = _apply(
                conn,
                request,
                correlation_id,
                knowledge,
                decisions,
                city,
                scenario,
                clock,
            )
    except DecisionRejectedError as exc:
        # C07 interprets Conflict only as a stale *plan*, never as a C10 key collision.
        return (
            exc.failure
            if exc.failure.kind == "Unavailable"
            else Failure("Invalid", exc.failure.message)
        )
    except psycopg.Error:
        return Failure("Unavailable", "C05.REPLAN_STORE_UNAVAILABLE")
    except ValidationError:
        return Failure("Unavailable", "C05.REPLAN_FACTS_INVALID")
    if isinstance(result, Failure):
        return result
    receipt = result.value
    trace = {
        "component": "C05",
        "correlationId": str(receipt.correlation_id),
        "scope": request.scope,
        "runId": str(request.run_id),
    }
    if receipt.event_id:
        trace.update(eventId=str(receipt.event_id), eventType="PlanReady")
    log.info(
        "requestReplan: %s plan %s v%d for source event %s%s",
        receipt.outcome.status,
        request.plan_id,
        receipt.outcome.new_plan_version or request.trigger.based_on_plan_version,
        receipt.source_event_id,
        " (replayed)" if receipt.replayed else "",
        extra=trace,
    )
    return Ok(receipt.outcome)
