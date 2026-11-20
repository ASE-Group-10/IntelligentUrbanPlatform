"""Contract tests: no database, broker, or real agent required."""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.components.c07_disruption_detection.contracts import (
    OperationalDisruption,
    parse_payload,
)
from app.components.c08_allocation.events import (
    OperationalDisruptionEvent,
    PlanReadyEvent,
)
from app.components.c08_allocation.models import (
    Allocation,
    AllocationProposal,
    Capacity,
    CapacityUnit,
    CommitmentStatus,
    PlanReadyPayload,
    ReceiptStatus,
    ResourceKind,
    ResourceRequirement,
    ResourceSnapshot,
)
from app.shared.contracts import EventEnvelope


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


EXAMPLES = (
    Path(__file__).resolve().parents[2] / "contracts/components/c08-allocation/examples"
)


def event_fixture(name: str) -> str:
    return (EXAMPLES / name).read_text(encoding="utf-8")


def test_plan_ready_uses_the_shared_envelope_and_typed_requirements() -> None:
    fixture = event_fixture("plan-ready-event.json")
    envelope = EventEnvelope.model_validate_json(fixture)
    ready = PlanReadyEvent.model_validate_json(fixture)
    assert ready.payload.plan_id in envelope.affected.plan_ids
    assert envelope.entity_versions is not None
    assert (
        ready.payload.plan_version
        == envelope.entity_versions[str(ready.payload.plan_id)]
    )
    assert json.loads(
        ready.model_dump_json(by_alias=True, exclude_unset=True)
    ) == json.loads(fixture)


def test_c07_accepts_c08_operational_disruption() -> None:
    fixture = event_fixture("operational-disruption-event.json")
    disruption = OperationalDisruptionEvent.model_validate_json(fixture)
    wire = disruption.model_dump_json(by_alias=True, exclude_unset=True)
    assert json.loads(wire) == json.loads(fixture)
    received = parse_payload(EventEnvelope.model_validate_json(wire))
    assert isinstance(received, OperationalDisruption)
    assert received.failure_id == disruption.payload.failure_id
    assert (
        received.committed_state.released == disruption.payload.committed_state.released
    )


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"schemaVersion": 2}, "schemaVersion"),
        ({"producer": "C08"}, "producer"),
        ({"eventType": "OperationalDisruption"}, "eventType"),
        ({"payload": {}}, "payload"),
        ({"scope": "simulation"}, "runId"),
        ({"runId": "a7d9e2f4-3c1b-4b8a-9e6f-0d1c2b3a4f5e"}, "runId"),
    ],
)
def test_plan_ready_rejects_invalid_event(
    overrides: dict[str, object], field: str
) -> None:
    wire = json.loads(event_fixture("plan-ready-event.json"))
    wire.update(overrides)
    with pytest.raises(ValidationError, match=field):
        PlanReadyEvent.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "affected",
            {
                "planIds": ["d614062e-450a-488b-a186-76d8020e3112"],
                "resourceIds": [],
                "segmentKeys": [],
            },
            "affected.planIds",
        ),
        ("entityVersions", None, "entityVersions"),
    ],
)
def test_plan_ready_requires_consistent_plan_metadata(
    field: str, value: object, error: str
) -> None:
    wire = json.loads(event_fixture("plan-ready-event.json"))
    wire[field] = value
    with pytest.raises(ValidationError, match=error):
        PlanReadyEvent.model_validate_json(json.dumps(wire))


def test_plan_ready_rejects_stale_envelope_plan_version() -> None:
    wire = json.loads(event_fixture("plan-ready-event.json"))
    plan_id = wire["payload"]["planId"]
    wire["entityVersions"][plan_id] = wire["payload"]["planVersion"] - 1
    with pytest.raises(ValidationError, match="entityVersions"):
        PlanReadyEvent.model_validate_json(json.dumps(wire))


def test_plan_ready_accepts_c05_route_only_revision() -> None:
    c05_examples = EXAMPLES.parent.parent / "c05-planning/examples"
    previous = json.loads((c05_examples / "confirmed-plan.json").read_text())
    revision = PlanReadyEvent.model_validate_json(
        (c05_examples / "plan-ready-revision-event.json").read_text()
    )
    assert str(revision.run_id) == previous["runScope"]["runId"]
    assert str(revision.payload.plan_id) == previous["planId"]
    assert revision.payload.plan_version == previous["version"] + 1
    assert (
        revision.payload.model_dump(mode="json")["requirements"]
        == previous["resourceNeeds"]
    )
