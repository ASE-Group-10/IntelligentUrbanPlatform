from datetime import datetime, time
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
ResourceType = Literal["vehicle", "depot", "warehouse", "locker"]
ProfileStatus = Literal["active", "inactive"]
Availability = Literal["available", "unavailable", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OperatingHours(StrictModel):
    day: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]
    opens: time
    closes: time

    @model_validator(mode="after")
    def valid_interval(self):
        if self.opens.tzinfo or self.closes.tzinfo or self.opens >= self.closes:
            raise ValueError("Use local opening/closing times within one day.")
        return self


class TemperatureRestriction(StrictModel):
    type: Literal["temperature_range"]
    min_celsius: float = Field(allow_inf_nan=False)
    max_celsius: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_range(self):
        if self.min_celsius > self.max_celsius:
            raise ValueError("Minimum temperature must not exceed maximum.")
        return self


class ZoneRestriction(StrictModel):
    type: Literal["operating_zone"]
    zone_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


Restriction = Annotated[
    TemperatureRestriction | ZoneRestriction, Field(discriminator="type")
]


class CommonDetails(StrictModel):
    timezone: str = "Europe/Dublin"
    operating_hours: list[OperatingHours] = Field(default_factory=list)
    restrictions: list[Restriction] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Use a valid IANA timezone.") from error
        return value


class VehicleDetails(CommonDetails):
    vehicle_category: Literal["van", "truck", "cargo_bike", "autonomous_vehicle"]
    max_weight_kg: Positive
    max_volume_m3: Positive | None = None
    powertrain: Literal["electric", "diesel", "petrol", "hybrid", "human"]
    emission_class: Annotated[str, Field(min_length=1, max_length=50)] | None = None
    range_km: Positive | None = None
    autonomous: bool = Field(default=False, strict=True)


class DepotDetails(CommonDetails):
    storage_capacity_m3: Positive
    loading_bays: int = Field(ge=0, strict=True)
    supported_vehicle_categories: list[
        Literal["van", "truck", "cargo_bike", "autonomous_vehicle"]
    ] = Field(min_length=1)


class WarehouseDetails(CommonDetails):
    storage_capacity_m3: Positive
    handling_capabilities: list[
        Literal["ambient", "chilled", "frozen", "fragile", "pallets"]
    ] = Field(min_length=1)


class Compartment(StrictModel):
    size: Literal["small", "medium", "large"]
    count: int = Field(gt=0, strict=True)
    length_cm: Positive
    width_cm: Positive
    height_cm: Positive


class LockerDetails(CommonDetails):
    public_access: bool = Field(strict=True)
    max_parcel_weight_kg: Positive
    compartments: list[Compartment] = Field(min_length=1)

    @field_validator("compartments")
    @classmethod
    def unique_sizes(cls, value):
        if len({item.size for item in value}) != len(value):
            raise ValueError("Each compartment size must occur once.")
        return value


DETAIL_MODELS = {
    "vehicle": VehicleDetails,
    "depot": DepotDetails,
    "warehouse": WarehouseDetails,
    "locker": LockerDetails,
}


class ResourceBase(StrictModel):
    external_identifier: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    profile_status: ProfileStatus = "active"
    observed_availability: Availability = "unknown"


class VehicleCreate(ResourceBase):
    resource_type: Literal["vehicle"]
    details: VehicleDetails


class DepotCreate(ResourceBase):
    resource_type: Literal["depot"]
    details: DepotDetails


class WarehouseCreate(ResourceBase):
    resource_type: Literal["warehouse"]
    details: WarehouseDetails


class LockerCreate(ResourceBase):
    resource_type: Literal["locker"]
    details: LockerDetails


ResourceCreate = Annotated[
    VehicleCreate | DepotCreate | WarehouseCreate | LockerCreate,
    Field(discriminator="resource_type"),
]
resource_adapter = TypeAdapter(ResourceCreate)


class ResourceUpdate(StrictModel):
    expected_version: int = Field(gt=0, strict=True)
    external_identifier: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    profile_status: ProfileStatus | None = None
    observed_availability: Availability | None = None
    details: dict | None = None

    @model_validator(mode="after")
    def valid_changes(self):
        changes = self.model_fields_set - {"expected_version"}
        if not changes:
            raise ValueError("Supply at least one field to update.")
        if any(getattr(self, name) is None for name in changes):
            raise ValueError("Updated fields cannot be null.")
        return self


class ActivationRequest(StrictModel):
    expected_version: int = Field(gt=0, strict=True)


class ResourceResponse(ResourceBase):
    id: UUID
    provider_id: UUID
    resource_type: ResourceType
    details: dict
    location_reference: str | None
    location_validated_at: datetime
    availability_observed_at: datetime | None
    agent_status: Literal["pending", "active", "failed"]
    agent_id: UUID | None
    version: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    planning_eligible: bool
    eligibility_reasons: list[str]


class ResourceList(StrictModel):
    items: list[ResourceResponse]
    next_cursor: UUID | None
