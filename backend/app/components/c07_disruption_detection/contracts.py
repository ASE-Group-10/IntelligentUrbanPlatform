"""C07 Disruption Detection contract models.

Includes the C05 calls C07 makes (findAffectedWork, requestReplan), the events
C07 consumes from C03/C08 (consumer-side copies until those pairs publish their
own), and the Disruption record C07 owns.
"""

from typing import Annotated, Literal, Self

from pydantic import UUID4, Field, model_validator

from app.components.c06_city_monitoring.contracts import CityStateUpdated, SegmentCause
from app.shared.contracts import (
    ContractModel,
    EventEnvelope,
    Quality,
    RunScope,
    SegmentKey,
    UtcDatetime,
    Version,
)

ReplanReason = Literal[
    "road_closed",
    "congestion",
    "resource_unavailable",
    "operational_failure",
    "weather",
]
ReplanOutcomeStatus = Literal["NoChange", "ReviewRequired", "PlanSaved"]
"""requestReplan is synchronous: C05 always answers with a final outcome."""
DisruptionStatus = Literal["active", "resolved", "unknown"]
DisruptionCause = Literal[SegmentCause, "resource", "operational"]
Severity = Literal["low", "medium", "high"]
ResourceType = Literal["vehicle", "depot", "warehouse", "locker"]
ResourceStatus = Literal["available", "limited", "unavailable", "unknown"]
FailureKind = Literal[
    "commit_conflict",
    "capacity_exceeded",
    "resource_lost",
    "driver_unavailable",
    "agent_timeout",
]


def _check_run_scope(scope: RunScope, run_id: UUID4 | None) -> None:
    """Same rule as ``EventEnvelope``: ``runId`` only, and always, in simulation."""
    if scope == "simulation" and run_id is None:
        raise ValueError("runId is required when scope is 'simulation'")
    if scope == "live" and run_id is not None:
        raise ValueError("runId must be absent when scope is 'live'")


class ChangeScope(ContractModel):
    """Request for C05 ``findAffectedWork``: what changed, and when.

    ``scope`` and ``run_id`` are copied from the event that triggered the
    detection, so a simulation only ever resolves its own run's plans.
    """

    segment_keys: list[SegmentKey] = Field(default_factory=list)
    resource_ids: list[UUID4] = Field(default_factory=list)
    plan_ids: list[UUID4] = Field(default_factory=list)
    from_: UtcDatetime | None = Field(default=None, alias="from")
    to: UtcDatetime | None = None
    scope: RunScope
    run_id: UUID4 | None = None

    @model_validator(mode="after")
    def _names_something(self) -> Self:
        if not (self.segment_keys or self.resource_ids or self.plan_ids):
            raise ValueError("provide segmentKeys, resourceIds or planIds")
        return self

    @model_validator(mode="after")
    def _run_id_fits_scope(self) -> Self:
        _check_run_scope(self.scope, self.run_id)
        return self


class AffectedWorkItem(ContractModel):
    """One plan touching the change scope, at the version C05 checked."""

    plan_id: UUID4
    plan_version: Version
    delivery_ids: list[UUID4]
    matched_segment_keys: list[SegmentKey] | None = None
    matched_resource_ids: list[UUID4] | None = None


class AffectedWork(ContractModel):
    """Response from C05 ``findAffectedWork``; empty ``items`` = nothing affected."""

    items: list[AffectedWorkItem]


class ReplanTrigger(ContractModel):
    """Why C07 is asking C05 to replan a plan."""

    disruption_id: UUID4
    disruption_version: Version
    based_on_plan_version: Version
    reason: ReplanReason
    city_snapshot_version: Version | None = None
    source_event_id: UUID4


def replan_key(plan_id: UUID4, trigger: ReplanTrigger) -> str:
    """Idempotency key ``planId:basedOnPlanVersion:disruptionId:disruptionVersion``."""
    return (
        f"{plan_id}:{trigger.based_on_plan_version}:"
        f"{trigger.disruption_id}:{trigger.disruption_version}"
    )


class ReplanRequest(ContractModel):
    """Body of C05 ``requestReplan`` (the AuthContext travels separately).

    ``scope`` and ``run_id`` are copied from the event that triggered the
    detection; the key does not include them.

    Raises:
        ValidationError: if ``key`` does not match plan, plan version and
            disruption version, or if ``run_id`` does not fit ``scope``.
    """

    plan_id: UUID4
    trigger: ReplanTrigger
    scope: RunScope
    run_id: UUID4 | None = None
    key: str

    @classmethod
    def build(
        cls,
        plan_id: UUID4,
        trigger: ReplanTrigger,
        scope: RunScope,
        run_id: UUID4 | None = None,
    ) -> Self:
        """Create a request with its idempotency key derived from the trigger."""
        return cls(
            plan_id=plan_id,
            trigger=trigger,
            scope=scope,
            run_id=run_id,
            key=replan_key(plan_id, trigger),
        )

    @model_validator(mode="after")
    def _key_matches(self) -> Self:
        if self.key != replan_key(self.plan_id, self.trigger):
            raise ValueError(
                "key must be planId:basedOnPlanVersion:disruptionId:disruptionVersion"
            )
        return self

    @model_validator(mode="after")
    def _run_id_fits_scope(self) -> Self:
        _check_run_scope(self.scope, self.run_id)
        return self


class ReplanOutcome(ContractModel):
    """Response from C05 ``requestReplan``.

    ``ReviewRequired`` means the revision is infeasible without an operator
    decision; ``reason`` says why.
    """

    status: ReplanOutcomeStatus
    new_plan_version: Version | None = None
    decision_id: UUID4 | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _saved_has_version_and_decision(self) -> Self:
        if self.status == "PlanSaved" and (
            self.new_plan_version is None or self.decision_id is None
        ):
            raise ValueError("PlanSaved requires newPlanVersion and decisionId")
        return self

    @model_validator(mode="after")
    def _review_has_reason(self) -> Self:
        if self.status == "ReviewRequired" and self.reason is None:
            raise ValueError("ReviewRequired requires a reason")
        return self


class AffectedPlan(ContractModel):
    """A plan a disruption touches and what happened to its replan request."""

    plan_id: UUID4
    plan_version: Version
    replan_requested: bool
    replan_outcome: ReplanOutcomeStatus | None = None


class Disruption(ContractModel):
    """Disruption record owned by C07; ``version`` bumps only on material change."""

    disruption_id: UUID4
    version: Version
    status: DisruptionStatus
    cause: DisruptionCause
    severity: Severity
    segment_keys: list[SegmentKey]
    resource_ids: list[UUID4]
    affected_plans: list[AffectedPlan]
    trigger_event_ids: list[UUID4]
    quality: Quality
    created_at: UtcDatetime
    updated_at: UtcDatetime


class Location(ContractModel):
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]


class ResourceAvailabilityChanged(ContractModel):
    """Payload of C03 ``ResourceAvailabilityChanged`` (consumer-side copy)."""

    resource_id: UUID4
    resource_type: ResourceType
    resource_version: Version
    previous_status: ResourceStatus
    new_status: ResourceStatus
    reason: str | None = None
    location: Location | None = None
    capacity_change: dict[str, int] | None = None  # slice: no
    expected_back_at: UtcDatetime | None = None
    quality: Quality


class CommittedState(ContractModel):
    still_committed: list[UUID4]
    released: list[UUID4]


class OperationalDisruption(ContractModel):
    """Payload of C08 ``OperationalDisruption`` (consumer-side copy).

    ``failure_id`` is the trigger identity C07 deduplicates on.
    """

    failure_id: UUID4
    failure_kind: FailureKind
    plan_id: UUID4
    plan_version: Version
    delivery_ids: list[UUID4]
    resource_ids: list[UUID4]
    committed_state: CommittedState
    decision_id: UUID4 | None = None
    detail: str | None = None


CONSUMED_PAYLOADS: dict[str, type[ContractModel]] = {
    "CityStateUpdated": CityStateUpdated,
    "ResourceAvailabilityChanged": ResourceAvailabilityChanged,
    "OperationalDisruption": OperationalDisruption,
}


def parse_payload(envelope: EventEnvelope) -> ContractModel:
    """Validate an envelope's payload against the model for its ``eventType``.

    Raises:
        ValueError: if C07 does not consume this event type.
        ValidationError: if the payload does not match its contract.
    """
    model = CONSUMED_PAYLOADS.get(envelope.event_type)
    if model is None:
        raise ValueError(f"C07 does not consume {envelope.event_type!r}")
    return model.model_validate(envelope.payload)
