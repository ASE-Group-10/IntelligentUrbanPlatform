"""Proposed C11 KPI value contracts; no operational writes or adapters."""

from typing import Annotated, Any, Literal, Self

from pydantic import UUID4, Field, model_validator

from app.shared.contracts import ContractModel, UtcDatetime

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Count = Annotated[int, Field(ge=0, strict=True)]
Number = Annotated[float, Field(allow_inf_nan=False)]


class TimeRange(ContractModel):
    """Half-open UTC range [start, end)."""

    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must precede end")
        return self


class AnalysisScope(ContractModel):
    """Requested scope; C02 must authorise it, never trust client provider IDs."""

    period: TimeRange
    provider_id: UUID4
    area_id: Identifier | None = None
    resource_ids: list[UUID4] = Field(default_factory=list)
    scope: Literal["live", "simulation"]
    run_id: UUID4 | None = None

    @model_validator(mode="after")
    def run_matches_scope(self) -> Self:
        if (self.scope == "simulation") != (self.run_id is not None):
            raise ValueError("runId is required only for simulation scope")
        return self


class Coverage(ContractModel):
    """Counts refer to the population defined by metricDefinitionVersion."""

    eligible: Count
    observed: Count
    excluded: Count = 0

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.observed > self.eligible:
            raise ValueError("observed cannot exceed eligible")
        return self


class Indicator(ContractModel):
    metric_id: Identifier
    value: Number | None
    unit: Identifier
    status: Literal["available", "partial", "unavailable"]
    coverage: Coverage
    last_updated: UtcDatetime | None
    estimated: bool
    estimation_basis: Identifier | None = None
    reason: Identifier | None = None

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        if self.status == "unavailable":
            if self.value is not None or not self.reason:
                raise ValueError("unavailable requires null value and reason")
        else:
            if self.value is None or self.last_updated is None:
                raise ValueError("usable values require value and lastUpdated")
            if self.coverage.observed == 0:
                raise ValueError("no observations cannot produce a value")
            if self.status == "available" and (
                self.coverage.observed != self.coverage.eligible
            ):
                raise ValueError("incomplete coverage must be partial")
        if self.estimated and not self.estimation_basis:
            raise ValueError("estimated indicators require a versioned basis")
        return self


class TrendPoint(ContractModel):
    period: TimeRange
    indicator: Indicator


class KPIQuery(ContractModel):
    scope: AnalysisScope
    metric_definition_version: Identifier


class KPISet(KPIQuery):
    schema_version: Literal[1] = 1
    generated_at: UtcDatetime
    status: Literal["current", "partial", "stale"]
    indicators: list[Indicator] = Field(min_length=1)
    trends: list[TrendPoint] = Field(default_factory=list)
    failure: Identifier | None = None

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        ids = [item.metric_id for item in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("metricId must be unique in a KPI set")
        if self.status == "stale" and not self.failure:
            raise ValueError("stale response requires refresh failure reason")
        if self.status != "stale" and self.failure:
            raise ValueError("failure belongs to a stale response")
        if self.status == "current" and any(
            item.status != "available" for item in self.indicators
        ):
            raise ValueError("missing/partial indicators require partial status")
        return self


class Breakdown(KPIQuery):
    metric_id: Identifier
    records: list[dict[str, Any]]
    source_event_ids: list[UUID4]
    next_cursor: Identifier | None = None
