"""UC06 ``evaluateChange``: decide whether a City State change disrupts any plan.

Slice scope: only ``CityStateUpdated`` events with ``closed`` segments are
evaluated. Everything else is reported as ``unknown`` or ``no_disruption``.
"""

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
    ReplanOutcomeStatus,
    ReplanRequest,
    ReplanTrigger,
    Severity,
    parse_payload,
)
from app.components.c07_disruption_detection.planning import (
    PlanningPort,
    PlanningUnavailableError,
)
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
    """Result of evaluating one event; ``replan_requests`` are those sent to C05."""

    kind: DetectionKind
    event_id: UUID4
    disruption_id: UUID4 | None = None
    disruption_version: Version | None = None
    replan_requests: list[ReplanRequest] = Field(default_factory=list)
    reason: str | None = None
    retry_later: bool = False
    """The event was not processed because a dependency (C05) was down;
    delivering it again later can succeed."""


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
            ``reason`` says how many are still pending if C05 refused some;
            ``Ok(kind="no_disruption")`` if no closed segment is used by a plan
            (AF2; the event is marked processed);
            ``Ok(kind="duplicate")`` if the event was already processed;
            ``Ok(kind="unknown")`` if reliable detection is not possible: the
            event type is not handled yet, a closed segment is not ``good``
            quality (AF1), C05 is unavailable, or the affected segments have no
            single known cause. Nothing is saved and the event is not marked
            processed, so a redelivery can succeed later;
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

        found = self._find_affected_work(closed)
        if found is None:
            return Ok(
                _outcome(
                    envelope, "unknown", reason="C05 is unavailable", retry_later=True
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
        self, closed: list[SegmentCondition]
    ) -> list[tuple[SegmentCondition, AffectedWork]] | None:
        """Step 6: ask C05, one call per closed segment; ``None`` if C05 is down."""
        found = []
        for condition in closed:
            scope = ChangeScope(segment_keys=[condition.segment_key])
            try:
                found.append((condition, self._planning.find_affected_work(scope)))
            except PlanningUnavailableError:
                return None
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

        A plan whose request C05 cannot take stays ``replan_requested=False``;
        the others continue. No retry here.
        """
        disruption = detected.disruption
        sent: list[ReplanRequest] = []
        statuses: dict[UUID, ReplanOutcomeStatus] = {}
        for plan in disruption.affected_plans:
            request = ReplanRequest.build(
                plan.plan_id,
                ReplanTrigger(
                    disruption_id=disruption.disruption_id,
                    disruption_version=disruption.version,
                    based_on_plan_version=plan.plan_version,
                    reason="road_closed",
                    city_snapshot_version=detected.snapshot_version,
                    source_event_id=envelope.event_id,
                ),
            )
            try:
                outcome = self._planning.request_replan(
                    request, envelope.correlation_id
                )
            except PlanningUnavailableError:
                continue
            sent.append(request)
            statuses[plan.plan_id] = outcome.status

        if statuses:
            self._record_replans(envelope, disruption, statuses)

        pending = len(disruption.affected_plans) - len(sent)
        return DetectionOutcome(
            kind="created",
            event_id=envelope.event_id,
            disruption_id=disruption.disruption_id,
            disruption_version=disruption.version,
            replan_requests=sent,
            reason=f"{pending} replan request(s) pending: C05 unavailable"
            if pending
            else None,
        )

    def _record_replans(
        self,
        envelope: EventEnvelope,
        disruption: Disruption,
        statuses: dict[UUID, ReplanOutcomeStatus],
    ) -> None:
        """Store each replan outcome. Not a material change: version stays the same."""
        plans = [
            plan.model_copy(
                update={
                    "replan_requested": True,
                    "replan_outcome": statuses[plan.plan_id],
                }
            )
            if plan.plan_id in statuses
            else plan
            for plan in disruption.affected_plans
        ]
        updated = disruption.model_copy(
            update={"affected_plans": plans, "updated_at": self._clock()}
        )
        with self._store.transaction(envelope.scope, envelope.run_id) as tx:
            tx.save_disruption(updated)


def _outcome(
    envelope: EventEnvelope,
    kind: DetectionKind,
    reason: str | None = None,
    retry_later: bool = False,
) -> DetectionOutcome:
    """An outcome that created nothing (no disruption, no replan)."""
    return DetectionOutcome(
        kind=kind, event_id=envelope.event_id, reason=reason, retry_later=retry_later
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
