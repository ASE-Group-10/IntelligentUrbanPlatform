"""Typed models for C04 Delivery Management contracts.

Matches ``contracts/components/c04-delivery-management/`` (``openapi.yaml``,
``internal.md``, and ``events.yaml``). Wire format is camelCase JSON; Python
attributes are snake_case. Unknown fields are rejected.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import (
    UUID4,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.shared.contracts import (
    ContractModel,
    EventMetadata,
    RunScopeSelector,
    UtcDatetime,
    Version,
)

DeliveryStatus = Literal["pending", "planned", "in_progress", "delivered", "cancelled"]
ProgressStatus = Literal["pending", "picked_up", "in_transit", "delivered", "failed"]
VehicleKind = Literal["van", "truck", "cargo_bike"]
DeliveryEventType = Literal[
    "DeliverySubmitted", "DeliveryUpdated", "DeliveryProgressChanged"
]


def _non_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class DeliveryLocation(ContractModel):
    address: str = Field(min_length=1, max_length=500)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    instructions: str | None = Field(default=None, max_length=1000)

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        return _non_blank(value)


class DeliveryCargo(ContractModel):
    description: str = Field(min_length=1, max_length=500)
    weight_kg: float = Field(gt=0, allow_inf_nan=False)
    parcel_count: StrictInt = Field(ge=1)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _non_blank(value)


class DeliveryTimeWindow(ContractModel):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("timeWindow.end must be after timeWindow.start")
        return self


class TemperatureRange(ContractModel):
    minimum: float = Field(allow_inf_nan=False)
    maximum: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def _valid_bounds(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("temperatureRangeC.minimum must not exceed maximum")
        return self


class DeliveryConstraints(ContractModel):
    vehicle_kinds: list[VehicleKind] = Field(min_length=1)
    fragile: StrictBool
    temperature_range_c: TemperatureRange | None = None


class DeliveryProgress(ContractModel):
    status: ProgressStatus
    updated_at: UtcDatetime
    note: str | None = None


class DeliveryFields(ContractModel):
    pickup: DeliveryLocation
    destination: DeliveryLocation
    cargo: DeliveryCargo
    time_window: DeliveryTimeWindow
    constraints: DeliveryConstraints


class SubmitDeliveryRequest(DeliveryFields):
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _non_blank(value)


class UpdateDeliveryRequest(DeliveryFields):
    expected_version: Version
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _non_blank(value)


class Delivery(DeliveryFields):
    delivery_id: UUID4
    version: Version
    status: DeliveryStatus
    planned_at: UtcDatetime | None = None
    progress: DeliveryProgress
    created_at: UtcDatetime
    updated_at: UtcDatetime

    def to_wire(self) -> dict:
        """Serialise to the canonical C04Delivery JSON dict."""
        data = self.model_dump(mode="json", by_alias=True)
        for loc_key in ("pickup", "destination"):
            if data[loc_key].get("instructions") is None:
                data[loc_key].pop("instructions", None)
        if data["constraints"].get("temperatureRangeC") is None:
            data["constraints"].pop("temperatureRangeC", None)
        return data


class DeliveryPage(ContractModel):
    items: list[Delivery] = Field(max_length=100)
    next_cursor: str | None = None


class DeliveryLinkReceipt(ContractModel):
    request_ids: list[UUID]
    plan_id: UUID
    versions: dict[UUID, Version]


class DeliveryEventPayload(ContractModel):
    delivery_id: UUID4
    delivery_version: Version
    provider_id: UUID4
    status: DeliveryStatus
    delivery: Delivery


class DeliveryEvent(EventMetadata):
    event_type: DeliveryEventType
    producer: Literal["C04"] = "C04"
    entity_versions: dict[str, Version]
    payload: DeliveryEventPayload


def scope_key_for(run_scope: RunScopeSelector) -> str:
    """Canonical partition key matching C06/C07 (``live`` or ``simulation:<uuid>``)."""
    if run_scope.scope == "live":
        return "live"
    return f"simulation:{run_scope.run_id}"
