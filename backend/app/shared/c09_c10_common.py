"""Shared types for C09/C10 contracts (contract doc §3, §7.1).

JSON uses camelCase; Python attributes use snake_case via aliases.
extra="forbid" so any field drift between producer and consumer fails loudly.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class Outcome(StrEnum):
    OK = "Ok"
    INVALID = "Invalid"
    FORBIDDEN = "Forbidden"
    NOT_FOUND = "NotFound"
    CONFLICT = "Conflict"
    UNAVAILABLE = "Unavailable"
    INFEASIBLE = "Infeasible"
    PARTIAL = "Partial"
    UNCONFIRMED = "Unconfirmed"


# shared with C03/C04/C06 — pending confirmation (doc §2 item 14)
class VehicleClass(StrEnum):
    """Team-agreed vehicle types, shared with C04 (#68)."""

    VAN = "van"
    TRUCK = "truck"
    CARGO_BIKE = "cargo_bike"


class Activity(StrEnum):
    PICKUP = "Pickup"
    DELIVERY = "Delivery"
    TRANSIT = "Transit"
    LOADING = "Loading"


class PrincipalType(StrEnum):
    USER = "User"
    SERVICE = "Service"


class AuthScope(Model):
    provider_ids: list[str] | None = None
    zone_ids: list[str] | None = None


class AuthContext(Model):
    principal_id: str
    principal_type: PrincipalType
    roles: list[str]
    scope: AuthScope | None = None
    correlation_id: UUID


class RunMode(StrEnum):
    LIVE = "Live"
    SIMULATION = "Simulation"


class RunScope(Model):
    mode: RunMode
    run_id: UUID | None

    @model_validator(mode="after")
    def _simulation_needs_run_id(self):
        if self.mode is RunMode.SIMULATION and self.run_id is None:
            raise ValueError("runId is required when mode is Simulation")
        return self


class ErrorBody(Model):
    outcome: Outcome
    code: str = Field(pattern=r"^C\d{2}\.[A-Z_]+$")
    message: str
    details: dict[str, Any] | None = None
    correlation_id: UUID


class TimeWindow(Model):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end <= self.start:
            raise ValueError("timeWindow.end must be after start")
        return self
