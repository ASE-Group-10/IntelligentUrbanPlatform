"""Version 1 C08 event boundaries, built on the backend's shared envelope."""

from typing import Literal, Self

from pydantic import UUID4, model_validator

from app.shared.contracts import ContractModel, EventMetadata, Version

from .models import PlanReadyPayload

FailureKind = Literal[
    "commit_conflict",
    "capacity_exceeded",
    "resource_lost",
    "driver_unavailable",
    "agent_timeout",
]


class CommittedState(ContractModel):
    still_committed: list[UUID4]
    released: list[UUID4]


class OperationalDisruptionPayload(ContractModel):
    failure_id: UUID4
    failure_kind: FailureKind
    plan_id: UUID4
    plan_version: Version
    delivery_ids: list[UUID4]
    resource_ids: list[UUID4]
    committed_state: CommittedState
    decision_id: UUID4 | None = None
    detail: str | None = None


class PlanReadyEvent(EventMetadata):
    event_type: Literal["PlanReady"]
    producer: Literal["C05"]
    schema_version: Literal[1]
    payload: PlanReadyPayload

    @model_validator(mode="after")
    def _check_plan_metadata(self) -> Self:
        plan_id = self.payload.plan_id
        if self.affected.plan_ids != [plan_id]:
            raise ValueError("affected.planIds must contain exactly payload.planId")
        versions = self.entity_versions or {}
        if versions.get(str(plan_id)) != self.payload.plan_version:
            raise ValueError(
                "entityVersions must match payload.planVersion for payload.planId"
            )
        return self


class OperationalDisruptionEvent(EventMetadata):
    event_type: Literal["OperationalDisruption"]
    producer: Literal["C08"]
    schema_version: Literal[1]
    payload: OperationalDisruptionPayload
