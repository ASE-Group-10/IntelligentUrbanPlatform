"""Cross-pair contract building blocks: conventions and the EventEnvelope.

Wire format is camelCase JSON; Python attributes are snake_case. Every model
rejects unknown fields so contract drift fails loudly in tests.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import (
    UUID4,
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic.alias_generators import to_camel


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC (use the 'Z' suffix)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_require_utc)]
"""ISO 8601 timestamp in UTC; naive or non-UTC values are invalid."""

Version = Annotated[int, Field(ge=1)]
"""Entity version: starts at 1 and increments on every committed change."""

SegmentKey = Annotated[str, Field(pattern=r"^seg:[1-9][0-9]*$")]
"""Road segment key ``seg:<osm_way_id>`` (OpenStreetMap way ID): ``seg:4471023``."""

Quality = Literal["good", "degraded", "unknown"]
RunScope = Literal["live", "simulation"]


class ContractModel(BaseModel):
    """Base for all contract models: camelCase aliases, unknown fields rejected."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class Affected(ContractModel):
    """Entities an event touches; empty lists are allowed."""

    plan_ids: list[UUID4]
    resource_ids: list[UUID4]
    segment_keys: list[SegmentKey]


class EventEnvelope(ContractModel):
    """Versioned JSON envelope for every RabbitMQ message.

    Consumers deduplicate on ``event_id``. ``run_id`` is required for
    ``simulation`` scope and forbidden for ``live`` so replayed events can
    never be mistaken for live ones.

    Raises:
        ValidationError: on missing fields, bad timestamps or scope/runId mismatch.
    """

    event_id: UUID4
    event_type: str = Field(min_length=1)
    schema_version: Version
    producer: str = Field(min_length=1)
    observed_at: UtcDatetime
    emitted_at: UtcDatetime
    correlation_id: UUID4
    causation_id: UUID4 | None = None
    affected: Affected
    entity_versions: dict[str, Version] | None = None
    scope: RunScope
    run_id: UUID4 | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_run_scope(self) -> Self:
        if self.scope == "simulation" and self.run_id is None:
            raise ValueError("runId is required when scope is 'simulation'")
        if self.scope == "live" and self.run_id is not None:
            raise ValueError("runId must be absent when scope is 'live'")
        return self
