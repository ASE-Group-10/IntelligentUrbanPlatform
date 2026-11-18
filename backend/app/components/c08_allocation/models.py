"""C08 shared schemas. These are proposals pending cross-pair agreement."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from app.shared.contracts import ContractModel as SharedContractModel

IdempotencyKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]
PositiveVersion = Annotated[int, Field(ge=1, strict=True)]
NonNegativeCapacity = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveCapacity = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ContractModel(SharedContractModel):
    """Reject unexpected fields and implicit data coercions at trust boundaries."""

    model_config = ConfigDict(strict=True, serialize_by_alias=True)


class ResourceKind(StrEnum):
    VAN = "van"
    CARGO_BIKE = "cargo_bike"
    DEPOT = "depot"
    WAREHOUSE = "warehouse"
    LOCKER = "locker"


class CapacityUnit(StrEnum):
    KG = "kg"
    PARCELS = "parcels"
    SLOTS = "slots"


class CommitmentStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    FAILED = "failed"


class AcceptanceStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    AWAITING_UC04_ACCEPTANCE = "awaiting_uc04_acceptance"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DriverResponse(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class ReceiptStatus(StrEnum):
    PENDING = "pending"
    RECEIVED = "received"


class OutcomeCode(StrEnum):
    CONFIRMED = "confirmed"
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    ASSIGNED = "assigned"
    ALLOCATION_FAILED = "allocation_failed"
    UNASSIGNED = "unassigned"
    UNRESOLVED = "unresolved"
    CONFLICT = "conflict"
    FORBIDDEN = "forbidden"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class Capacity(ContractModel):
    amount: PositiveCapacity
    unit: CapacityUnit


class ResourceRequirement(ContractModel):
    delivery_id: UUID
    resource_kind: ResourceKind
    capacity: Capacity


class PlanReadyPayload(ContractModel):
    plan_id: UUID
    plan_version: PositiveVersion
    requirements: list[ResourceRequirement] = Field(min_length=1)
    decision_id: UUID


class ResourceSnapshot(ContractModel):
    resource_id: UUID
    provider_id: UUID
    kind: ResourceKind
    resource_version: PositiveVersion
    eligible: bool
    available: bool
    total_capacity: Capacity
    committed_capacity: NonNegativeCapacity

    @model_validator(mode="after")
    def capacity_within_total(self) -> ResourceSnapshot:
        if self.committed_capacity > self.total_capacity.amount:
            raise ValueError("committed capacity exceeds total capacity")
        return self


class ExpectedVersions(ContractModel):
    plan_version: PositiveVersion
    resource_versions: dict[UUID, PositiveVersion] = Field(min_length=1)


class AllocationProposal(ContractModel):
    proposal_id: UUID
    negotiation_id: UUID
    agent_id: UUID
    resource_id: UUID
    delivery_id: UUID
    capacity: Capacity
    score: float = Field(ge=0, allow_inf_nan=False)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> AllocationProposal:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must have a timezone offset")
        return self


class Reservation(ContractModel):
    assignment_ref: UUID
    plan_id: UUID
    plan_version: PositiveVersion
    delivery_id: UUID
    resource_id: UUID
    capacity: Capacity
    status: CommitmentStatus
    expires_at: datetime


class Allocation(ContractModel):
    allocation_id: UUID
    assignment_ref: UUID
    plan_id: UUID
    plan_version: PositiveVersion
    delivery_id: UUID
    resource_id: UUID
    capacity: Capacity
    commitment_status: CommitmentStatus
    acceptance_status: AcceptanceStatus
    receipt_status: ReceiptStatus


class DriverOffer(ContractModel):
    offer_id: UUID
    assignment_ref: UUID
    driver_id: UUID
    expires_at: datetime
    status: AcceptanceStatus


class OfferResponseCommand(ContractModel):
    offer_id: UUID
    response: DriverResponse
    key: IdempotencyKey


class DecisionEvidenceSnapshot(ContractModel):
    """Exact evidence used for this decision, not a mutable C09 lookup key."""

    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    content: str = Field(min_length=1)
    captured_at: datetime


class AllocationOutcome(ContractModel):
    code: OutcomeCode
    correlation_id: UUID
    assignment_ref: UUID | None = None
    allocation_id: UUID | None = None
    resource_id: UUID | None = None
    resulting_plan_version: PositiveVersion | None = None
    resulting_resource_versions: dict[UUID, PositiveVersion] = Field(
        default_factory=dict
    )
    acceptance_status: AcceptanceStatus | None = None
    reason: str | None = None


class AuthContext(ContractModel):
    principal_id: UUID
    roles: list[str] = Field(min_length=1)
    provider_scope: list[UUID]
    correlation_id: UUID


class AllocationProblem(ContractModel):
    plan_id: UUID
    plan_version: PositiveVersion
    requirements: list[ResourceRequirement] = Field(min_length=1)
    candidate_resources: list[ResourceSnapshot]
    proposal_deadline: datetime


class CommitNegotiationCommand(ContractModel):
    negotiation_id: UUID
    selected_proposal: AllocationProposal
    expected_versions: ExpectedVersions
    key: IdempotencyKey


class AssignVehicleCommand(ContractModel):
    context: AuthContext
    plan_id: UUID
    delivery_id: UUID
    expected_version: PositiveVersion
    key: IdempotencyKey


class CommitAssignmentCommand(ContractModel):
    assignment_ref: UUID
    expected_plan_version: PositiveVersion
    key: IdempotencyKey
