"""Proposed C12 simulation/replay contracts; no runtime or live adapters."""

from typing import Annotated, Any, Literal, Self

from pydantic import UUID4, Field, model_validator

from app.components.c11_kpi_analytics.contracts import Indicator, TimeRange
from app.shared.contracts import ContractModel, EventEnvelope, UtcDatetime

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Count = Annotated[int, Field(ge=0, strict=True)]
Speed = Annotated[float, Field(gt=0, le=16, allow_inf_nan=False)]


class SimulationRequest(ContractModel):
    scenario_id: Identifier
    seed: int = Field(ge=0, strict=True)
    configuration_version: Identifier
    idempotency_key: Identifier


class RunManifest(ContractModel):
    schema_version: Literal[1] = 1
    run_id: UUID4
    provider_id: UUID4
    scope: Literal["simulation"] = "simulation"
    scenario_id: Identifier
    seed: int = Field(ge=0, strict=True)
    configuration_version: Identifier
    status: Literal["recording", "completed", "incomplete"]
    initial_state_ref: Identifier
    initial_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_ranges: list[TimeRange]
    last_sequence: Count


class RecordingRecord(ContractModel):
    """Full source outcome; sequence is run-local, contiguous and one-based."""

    run_id: UUID4
    sequence: int = Field(ge=1, strict=True)
    simulation_time: UtcDatetime
    event: EventEnvelope

    @model_validator(mode="after")
    def isolated(self) -> Self:
        if self.event.scope != "simulation" or self.event.run_id != self.run_id:
            raise ValueError("recording must contain an event from the same run")
        return self


class Checkpoint(ContractModel):
    run_id: UUID4
    cursor: Count
    simulation_time: UtcDatetime
    state_ref: Identifier
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayRequest(ContractModel):
    run_id: UUID4
    time_range: TimeRange
    speed: Speed = 1


class ReplayControl(ContractModel):
    session_id: UUID4
    command: Literal["play", "pause", "resume", "seek", "stop"]
    expected_cursor: Count
    target_cursor: Count | None = None

    @model_validator(mode="after")
    def seek_target(self) -> Self:
        if (self.command == "seek") != (self.target_cursor is not None):
            raise ValueError("targetCursor is required only for seek")
        return self


class ReplayFrame(ContractModel):
    schema_version: Literal[1] = 1
    session_id: UUID4
    run_id: UUID4
    cursor: Count
    status: Literal["ready", "playing", "paused", "completed", "stopped"]
    simulation_time: UtcDatetime
    state: dict[str, Any]
    decision_ids: list[UUID4]
    recorded_kpis: list[Indicator]
    interruption: Identifier | None = None

    @model_validator(mode="after")
    def interruption_pauses(self) -> Self:
        if self.interruption and self.status != "paused":
            raise ValueError("interrupted playback must be paused")
        return self


class CannotReplay(ContractModel):
    run_id: UUID4
    reason: Identifier
    available_ranges: list[TimeRange]


class DecisionSelector(ContractModel):
    """C10 historical selector; no current C09 query is permitted."""

    run_id: UUID4
    decision_id: UUID4
