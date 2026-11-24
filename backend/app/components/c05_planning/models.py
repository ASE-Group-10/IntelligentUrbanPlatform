"""C05 plan views and the saved C04 facts used by the Dublin generator."""

import re
from datetime import datetime
from typing import Literal

from pydantic import UUID4, ConfigDict, Field, field_validator

from app.components.c08_allocation.models import ResourceRequirement
from app.shared.c09_c10_common import TimeWindow, VehicleClass
from app.shared.contracts import (
    ContractModel,
    RunScopeSelector,
    SegmentKey,
    UtcDatetime,
    Version,
)

_RFC3339_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
    re.ASCII,
)


class PlanScope(RunScopeSelector):
    """The run partition stored on a C05 plan."""


class _C04Facts(ContractModel):
    """Project C04's saved delivery onto the fields used for generation."""

    model_config = ConfigDict(extra="ignore")


class DeliveryCargo(_C04Facts):
    weight_kg: float = Field(strict=True, gt=0, allow_inf_nan=False)


class DeliveryConstraints(_C04Facts):
    vehicle_kinds: list[VehicleClass] = Field(min_length=1)


class DeliveryProgress(_C04Facts):
    status: Literal["pending", "picked_up", "in_transit", "delivered", "failed"]


class DeliveryWindow(TimeWindow):
    start: UtcDatetime
    end: UtcDatetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_timestamp(cls, value: object) -> datetime | str:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and _RFC3339_TIMESTAMP.fullmatch(value):
            return value
        raise ValueError("delivery window timestamps must be RFC 3339 date-times")


class SavedDelivery(_C04Facts):
    """Authoritative C04 snapshot obtained in the caller's authorised run."""

    delivery_id: UUID4
    version: Version = Field(strict=True)
    status: Literal["pending", "planned", "in_progress", "delivered", "cancelled"]
    progress: DeliveryProgress
    cargo: DeliveryCargo
    constraints: DeliveryConstraints
    time_window: DeliveryWindow


class Route(ContractModel):
    delivery_ids: list[UUID4] = Field(min_length=1)
    segment_keys: list[SegmentKey]
    distance_metres: int = Field(ge=0)
    estimated_duration_seconds: int = Field(ge=0)


class Plan(ContractModel):
    """Version returned by generation and by the scoped plan read."""

    plan_id: UUID4
    version: Version
    status: Literal["awaiting_confirmation", "confirmed"]
    request_ids: list[UUID4] = Field(min_length=1)
    run_scope: PlanScope
    routes: list[Route] = Field(min_length=1)
    resource_needs: list[ResourceRequirement] = Field(min_length=1)
    decision_id: UUID4
    input_versions: dict[str, Version]
