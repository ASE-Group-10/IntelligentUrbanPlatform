"""Contract tests: no database, broker, or real agent required."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.c08.models import (
    Allocation,
    AllocationProposal,
    Capacity,
    CapacityUnit,
    CommitmentStatus,
    EventEnvelope,
    ExecutionScope,
    PlanReadyPayload,
    ReceiptStatus,
    ResourceKind,
    ResourceRequirement,
    ResourceSnapshot,
)


def requirement() -> ResourceRequirement:
    return ResourceRequirement(
        delivery_id=uuid4(),
        resource_kind=ResourceKind.VAN,
        capacity=Capacity(amount=5.0, unit=CapacityUnit.PARCELS),
    )


def test_plan_ready_round_trip() -> None:
    payload = PlanReadyPayload(
        plan_id=uuid4(),
        plan_version=2,
        requirements=[requirement()],
        decision_id=uuid4(),
    )
    restored = PlanReadyPayload.model_validate_json(payload.model_dump_json())
    assert restored == payload


def test_negative_capacity_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Capacity(amount=-1.0, unit=CapacityUnit.KG)


def test_missing_plan_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanReadyPayload(
            plan_id=uuid4(), requirements=[requirement()], decision_id=uuid4()
        )


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Capacity(amount=1.0, unit=CapacityUnit.KG, surprise="ignored")


def test_stale_or_invalid_version_cannot_be_zero() -> None:
    with pytest.raises(ValidationError):
        PlanReadyPayload(
            plan_id=uuid4(),
            plan_version=0,
            requirements=[requirement()],
            decision_id=uuid4(),
        )


def test_committed_capacity_cannot_exceed_total() -> None:
    with pytest.raises(ValidationError):
        ResourceSnapshot(
            resource_id=uuid4(),
            provider_id=uuid4(),
            kind=ResourceKind.VAN,
            resource_version=1,
            eligible=True,
            available=True,
            total_capacity=Capacity(amount=5.0, unit=CapacityUnit.PARCELS),
            committed_capacity=6.0,
        )


def test_unrecognised_offer_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Allocation(
            allocation_id=uuid4(),
            assignment_ref=uuid4(),
            plan_id=uuid4(),
            plan_version=1,
            delivery_id=uuid4(),
            resource_id=uuid4(),
            capacity=Capacity(amount=1.0, unit=CapacityUnit.PARCELS),
            commitment_status=CommitmentStatus.COMMITTED,
            acceptance_status="agent_acknowledged",
            receipt_status=ReceiptStatus.PENDING,
        )


def test_naive_proposal_expiry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AllocationProposal(
            proposal_id=uuid4(),
            negotiation_id=uuid4(),
            agent_id=uuid4(),
            resource_id=uuid4(),
            delivery_id=uuid4(),
            capacity=Capacity(amount=1.0, unit=CapacityUnit.PARCELS),
            score=0.7,
            expires_at=datetime(2026, 11, 20),
        )


def event(**overrides: object) -> EventEnvelope:
    data = dict(
        schema_version=1,
        event_id=uuid4(),
        event_type="PlanReady",
        producer="c05",
        occurred_at=datetime.now(UTC),
        ingested_at=datetime.now(UTC),
        correlation_id=uuid4(),
        causation_id=None,
        scope=ExecutionScope.LIVE,
        payload={"plan_id": str(uuid4())},
    )
    data.update(overrides)
    return EventEnvelope(**data)


def test_envelope_json_round_trip() -> None:
    original = event()
    assert EventEnvelope.model_validate_json(original.model_dump_json()) == original


def test_unsupported_event_schema_is_rejected() -> None:
    with pytest.raises(ValidationError):
        event(schema_version=2)


def test_simulation_event_requires_run_id() -> None:
    with pytest.raises(ValidationError):
        event(scope=ExecutionScope.SIMULATION)


def test_live_event_disallows_run_id() -> None:
    with pytest.raises(ValidationError):
        event(run_id=uuid4())
