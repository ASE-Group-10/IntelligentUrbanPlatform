"""UC06 ``evaluateChange``: decide whether a City State change disrupts any plan.

Slice scope: only ``CityStateUpdated`` events with ``closed`` segments are
evaluated. Everything else is reported as ``unknown`` or ``no_disruption``.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import UUID4, Field, ValidationError

from app.components.c06_city_monitoring.contracts import (
    CityStateUpdated,
    SegmentCondition,
)
from app.components.c07_disruption_detection.contracts import (
    AffectedPlan,
    AffectedWork,
    ChangeScope,
    Disruption,
    DisruptionCause,
    ReplanOutcome,
    ReplanRequest,
    ReplanTrigger,
    Severity,
    parse_payload,
)
from app.components.c07_disruption_detection.planning import PlanningPort
from app.components.c07_disruption_detection.store import (
    DisruptionStore,
    DisruptionStoreUnavailableError,
    DisruptionTransaction,
)
from app.shared.contracts import ContractModel, EventEnvelope, Version
from app.shared.result import Failure, Ok, Result

DetectionKind = Literal["duplicate", "no_disruption", "unknown", "created"]
"""What ``evaluate_change`` concluded. ``updated`` is reserved for AF3."""

FIRST_VERSION = 1
SEVERITY_RANK: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2}


class DetectionOutcome(ContractModel):
    """Result of evaluating one event.

    ``replan_requests`` are the requests C05 accepted. ``retryable`` is true when
    redelivering the event later may succeed because C05 was unavailable.
    """

    kind: DetectionKind
    event_id: UUID4
    disruption_id: UUID4 | None = None
    disruption_version: Version | None = None
    replan_requests: list[ReplanRequest] = Field(default_factory=list)
    reason: str | None = None
    retryable: bool = False


def classify_impact(
    condition: SegmentCondition, affected: AffectedWork
) -> Severity | None:
    """Slice rule: a ``closed`` segment used by some plan is ``high``.

    Returns ``None`` for anything else (out of scope until Iteration 3).
    """
    if condition.status == "closed" and affected.items:
        return "high"
    return None


@dataclass(frozen=True)
class _Impact:
    """A closed segment that at least one plan uses, with its severity."""

    condition: SegmentCondition
    work: AffectedWork
    severity: Severity


@dataclass(frozen=True)
class _Detected:
    """A disruption saved in step 7, and the City State version it was judged on."""

    disruption: Disruption
    snapshot_version: Version


@dataclass(frozen=True)
class _Replan:
    """How the replan of one affected plan ended.

    ``plan`` is the entry to store; ``request`` is the request C05 accepted, or
    ``None`` with ``not_made`` saying why not.
    """

    plan: AffectedPlan
    request: ReplanRequest | None = None
    not_made: str | None = None


class DisruptionDetection:
    """C07 service API (UC06)."""

    def __init__(
        self,
        planning: PlanningPort,
        store: DisruptionStore,
        clock: Callable[[], datetime],
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._planning = planning
        self._store = store
        self._clock = clock
        self._new_id = new_id

    def evaluate_change(self, envelope: EventEnvelope) -> Result[DetectionOutcome]:
        """Evaluate one ``CityStateUpdated`` event and trigger replans (UC06).

        One event creates at most one disruption, grouping every closed segment
        that a plan uses, and one replan request per affected plan.

        Returns:
            ``Ok(kind="created")`` once the disruption is saved and replans were
            requested; ``replan_requests`` holds those C05 received, and
            ``reason`` says why each of the others was not made;
            ``Ok(kind="no_disruption")`` if no closed segment is used by a plan
            (AF2; the event is marked processed);
            ``Ok(kind="duplicate")`` if the event was already processed;
            ``Ok(kind="unknown")`` if reliable detection is not possible: the
            event type is not handled yet, a closed segment is not ``good``
            quality (AF1), ``findAffectedWork`` failed, or the affected segments
            have no single known cause. Nothing is saved and the event is not
            marked processed, so a redelivery can succeed later; ``retryable`` is
            true when C05 was ``Unavailable``;
            ``Failure("Invalid")`` if the payload does not match its contract;
            ``Failure("Unavailable")`` if the store fails.
        """
        try:
            with self._store.transaction(envelope.scope, envelope.run_id) as tx:
                detected = self._detect(tx, envelope)
            if not isinstance(detected, _Detected):
                return detected
            return Ok(self._request_replans(envelope, detected))
        except DisruptionStoreUnavailableError as error:
            return Failure("Unavailable", str(error))

    def _detect(
        self, tx: DisruptionTransaction, envelope: EventEnvelope
    ) -> Result[DetectionOutcome] | _Detected:
        """Steps 1-7: receive, validate, compare, classify and record."""
        if tx.is_processed(envelope.event_id):
            return Ok(_outcome(envelope, "duplicate"))

        payload = _read_payload(envelope)
        if not isinstance(payload, CityStateUpdated):
            return payload

        closed = _closed_changes(payload)
        if any(condition.quality != "good" for condition in closed):
            return Ok(
                _outcome(
                    envelope,
                    "unknown",
                    reason="a closed segment is not good quality",
                )
            )

        found = self._find_affected_work(envelope, closed)
        if isinstance(found, Failure):
            return Ok(
                _outcome(
                    envelope,
                    "unknown",
                    reason=f"findAffectedWork failed ({found.kind}): {found.message}",
                    retryable=found.kind == "Unavailable",
                )
            )

        impacts = _impacts(found)
        if not impacts:
            tx.mark_processed(envelope.event_id)
            return Ok(
                _outcome(
                    envelope,
                    "no_disruption",
                    reason="no closed segment is used by a plan",
                )
            )

        cause = _single_cause(impacts)
        if cause is None:
            return Ok(
                _outcome(
                    envelope,
                    "unknown",
                    reason="affected segments have no single known cause",
                )
            )

        disruption = self._record_disruption(tx, envelope, impacts, cause)
        return _Detected(disruption, payload.snapshot_version)

    def _find_affected_work(
        self, envelope: EventEnvelope, closed: list[SegmentCondition]
    ) -> list[tuple[SegmentCondition, AffectedWork]] | Failure:
        """Step 6: ask C05, one call per closed segment; stop at the first failure."""
        found = []
        for condition in closed:
            scope = _change_scope(envelope, [condition.segment_key])
            work = self._planning.find_affected_work(scope)
            if isinstance(work, Failure):
                return work
            found.append((condition, work.value))
        return found

    def _record_disruption(
        self,
        tx: DisruptionTransaction,
        envelope: EventEnvelope,
        impacts: list[_Impact],
        cause: DisruptionCause,
    ) -> Disruption:
        """Step 7: save one disruption and mark the event processed, together."""
        plans: dict[UUID, AffectedPlan] = {}
        for impact in impacts:
            for item in impact.work.items:
                known = plans.get(item.plan_id)
                version = item.plan_version
                if known is not None:
                    version = max(version, known.plan_version)
                plans[item.plan_id] = AffectedPlan(
                    plan_id=item.plan_id, plan_version=version, replan_requested=False
                )

        now = self._clock()
        disruption = Disruption(
            disruption_id=self._new_id(),
            version=FIRST_VERSION,
            status="active",
            cause=cause,
            severity=max((i.severity for i in impacts), key=SEVERITY_RANK.__getitem__),
            segment_keys=[impact.condition.segment_key for impact in impacts],
            resource_ids=[],
            affected_plans=list(plans.values()),
            trigger_event_ids=[envelope.event_id],
            quality="good",  # closed segments of any other quality ended earlier
            created_at=now,
            updated_at=now,
        )
        tx.save_disruption(disruption)
        tx.mark_processed(envelope.event_id)
        return disruption

    def _request_replans(
        self, envelope: EventEnvelope, detected: _Detected
    ) -> DetectionOutcome:
        """Step 9: one replan request per affected plan, after the commit.

        A plan whose request is not made stays ``replan_requested=False``; the
        others continue. No retry here beyond the stale-plan one.
        """
        disruption = detected.disruption
        replans = [
            self._replan_plan(envelope, detected, plan)
            for plan in disruption.affected_plans
        ]
        sent = [replan.request for replan in replans if replan.request is not None]
        if sent:
            self._record_replans(envelope, disruption, [r.plan for r in replans])

        not_made = Counter(r.not_made for r in replans if r.not_made is not None)
        reason = None
        if not_made:
            causes = ", ".join(f"{count} {cause}" for cause, count in not_made.items())
            reason = f"{not_made.total()} replan request(s) not made: {causes}"
        return DetectionOutcome(
            kind="created",
            event_id=envelope.event_id,
            disruption_id=disruption.disruption_id,
            disruption_version=disruption.version,
            replan_requests=sent,
            reason=reason,
        )

    def _replan_plan(
        self, envelope: EventEnvelope, detected: _Detected, plan: AffectedPlan
    ) -> _Replan:
        """Request the replan of one plan; only ``Conflict`` is handled specially."""
        request = self._build_request(envelope, detected, plan, plan.plan_version)
        result = self._planning.request_replan(request, envelope.correlation_id)
        if isinstance(result, Ok):
            return _accepted(plan, request, result.value)
        if result.kind == "Conflict":
            return self._retry_stale_plan(envelope, detected, plan)
        return _Replan(plan, not_made=result.kind)

    def _retry_stale_plan(
        self, envelope: EventEnvelope, detected: _Detected, plan: AffectedPlan
    ) -> _Replan:
        """A ``Conflict`` means the plan moved on: rediscover once, retry once.

        The retry uses the plan version C05 now reports, so its key is new. If the
        plan is no longer affected, or C05 reports the same version, nothing is
        retried.
        """
        scope = _change_scope(envelope, detected.disruption.segment_keys)
        found = self._planning.find_affected_work(scope)
        if isinstance(found, Failure):
            return _Replan(plan, not_made=found.kind)
        item = next((i for i in found.value.items if i.plan_id == plan.plan_id), None)
        if item is None:
            return _Replan(plan, not_made="no longer affected")
        if item.plan_version == plan.plan_version:
            return _Replan(plan, not_made="Conflict")

        request = self._build_request(envelope, detected, plan, item.plan_version)
        result = self._planning.request_replan(request, envelope.correlation_id)
        if isinstance(result, Failure):
            return _Replan(plan, not_made=result.kind)
        return _accepted(plan, request, result.value)

    def _build_request(
        self,
        envelope: EventEnvelope,
        detected: _Detected,
        plan: AffectedPlan,
        plan_version: int,
    ) -> ReplanRequest:
        disruption = detected.disruption
        return ReplanRequest.build(
            plan.plan_id,
            ReplanTrigger(
                disruption_id=disruption.disruption_id,
                disruption_version=disruption.version,
                based_on_plan_version=plan_version,
                reason="road_closed",
                city_snapshot_version=detected.snapshot_version,
                source_event_id=envelope.event_id,
            ),
            scope=envelope.scope,
            run_id=envelope.run_id,
        )

    def _record_replans(
        self,
        envelope: EventEnvelope,
        disruption: Disruption,
        plans: list[AffectedPlan],
    ) -> None:
        """Store each replan result. Not a material change: version stays the same."""
        updated = disruption.model_copy(
            update={"affected_plans": plans, "updated_at": self._clock()}
        )
        with self._store.transaction(envelope.scope, envelope.run_id) as tx:
            tx.save_disruption(updated)


def _outcome(
    envelope: EventEnvelope,
    kind: DetectionKind,
    reason: str | None = None,
    retryable: bool = False,
) -> DetectionOutcome:
    """An outcome that created nothing (no disruption, no replan)."""
    return DetectionOutcome(
        kind=kind, event_id=envelope.event_id, reason=reason, retryable=retryable
    )


def _change_scope(envelope: EventEnvelope, segment_keys: list[str]) -> ChangeScope:
    """``findAffectedWork`` request in the run scope of the triggering event."""
    return ChangeScope(
        segment_keys=segment_keys, scope=envelope.scope, run_id=envelope.run_id
    )


def _accepted(
    plan: AffectedPlan, request: ReplanRequest, outcome: ReplanOutcome
) -> _Replan:
    """C05 took the request: store the plan version it was based on and the status."""
    return _Replan(
        plan.model_copy(
            update={
                "plan_version": request.trigger.based_on_plan_version,
                "replan_requested": True,
                "replan_outcome": outcome.status,
            }
        ),
        request=request,
    )


def _read_payload(
    envelope: EventEnvelope,
) -> CityStateUpdated | Result[DetectionOutcome]:
    """Step 2: accept only ``CityStateUpdated`` and validate its payload."""
    if envelope.event_type != "CityStateUpdated":
        return Ok(_outcome(envelope, "unknown", reason="event type not handled yet"))
    try:
        payload = parse_payload(envelope)
    except ValidationError as error:
        return Failure("Invalid", f"CityStateUpdated payload is not valid: {error}")
    assert isinstance(payload, CityStateUpdated)
    return payload


def _closed_changes(payload: CityStateUpdated) -> list[SegmentCondition]:
    """Step 4 (first filter): the changes that closed a road, in payload order."""
    return [change for change in payload.changes if change.status == "closed"]


def _impacts(
    found: list[tuple[SegmentCondition, AffectedWork]],
) -> list[_Impact]:
    """Steps 4-5: keep the closed segments that disrupt some plan, with severity."""
    impacts = []
    for condition, work in found:
        severity = classify_impact(condition, work)
        if severity is not None:
            impacts.append(_Impact(condition, work, severity))
    return impacts


def _single_cause(impacts: list[_Impact]) -> DisruptionCause | None:
    """The one cause shared by all impacted segments; ``None`` if missing or mixed."""
    causes = {impact.condition.cause for impact in impacts}
    return causes.pop() if len(causes) == 1 else None
