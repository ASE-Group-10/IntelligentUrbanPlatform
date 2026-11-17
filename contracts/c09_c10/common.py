"""Shared types for C09/C10 contracts (contract doc §3, §7.1).

JSON uses camelCase; Python attributes use snake_case via aliases.
extra="forbid" so any field drift between producer and consumer fails loudly.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Outcome(str, Enum):
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
class VehicleClass(str, Enum):
    CARGO_BIKE = "CargoBike"
    VAN = "Van"
    LGV = "LGV"
    HGV = "HGV"


class Activity(str, Enum):
    PICKUP = "Pickup"
    DELIVERY = "Delivery"
    TRANSIT = "Transit"
    LOADING = "Loading"


class PrincipalType(str, Enum):
    USER = "User"
    SERVICE = "Service"


class AuthScope(Model):
    provider_ids: Optional[list[str]] = None
    zone_ids: Optional[list[str]] = None


class AuthContext(Model):
    principal_id: str
    principal_type: PrincipalType
    roles: list[str]
    scope: Optional[AuthScope] = None
    correlation_id: UUID


class RunMode(str, Enum):
    LIVE = "Live"
    SIMULATION = "Simulation"


class RunScope(Model):
    mode: RunMode
    run_id: Optional[UUID]

    @model_validator(mode="after")
    def _simulation_needs_run_id(self):
        if self.mode is RunMode.SIMULATION and self.run_id is None:
            raise ValueError("runId is required when mode is Simulation")
        return self


class ErrorBody(Model):
    outcome: Outcome
    code: str = Field(pattern=r"^C\d{2}\.[A-Z_]+$")
    message: str
    details: Optional[dict[str, Any]] = None
    correlation_id: UUID


class TimeWindow(Model):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end <= self.start:
            raise ValueError("timeWindow.end must be after start")
        return self
