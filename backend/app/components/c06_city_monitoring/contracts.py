"""C06 City Monitoring contract models (see contracts/components/c06-city-monitoring/).

Covers SegmentCondition, the CityStateUpdated payload, getCitySnapshot request and
response, and the ingestObservation input/receipt.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import UUID4, Field, model_validator

from app.shared.contracts import (
    ContractModel,
    Quality,
    SegmentKey,
    UtcDatetime,
    Version,
)

SegmentStatus = Literal["open", "slow", "closed"]
SegmentCause = Literal["traffic", "roadworks", "event", "weather", "incident", "manual"]
ObservationKind = Literal["traffic", "weather", "event", "closure"]
IngestionDisposition = Literal["merged", "history_only", "conflict", "rejected"]


class SegmentCondition(ContractModel):
    """Current condition of one road segment, as held in City State."""

    segment_key: SegmentKey
    status: SegmentStatus
    speed_kmh: Annotated[float, Field(ge=0)] | None = None
    delay_seconds: Annotated[int, Field(ge=0)] | None = None
    cause: SegmentCause | None = None
    valid_until: UtcDatetime | None = None
    observed_at: UtcDatetime
    quality: Quality
    sources: list[str] = Field(min_length=1)
    conflict: bool | None = None  # slice: no (AF3)


class CityStateUpdated(ContractModel):
    """Payload of the ``CityStateUpdated`` event: only the keys that changed."""

    snapshot_version: Version
    previous_version: Annotated[int, Field(ge=0)]
    changes: list[SegmentCondition]
    degraded: bool
    stale_sources: list[str]


class SnapshotScope(ContractModel):
    """Exactly one of ``segment_keys`` or ``bbox`` selects what to read."""

    segment_keys: list[SegmentKey] | None = None
    bbox: tuple[float, float, float, float] | None = (
        None  # minLon, minLat, maxLon, maxLat
    )

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> Self:
        if (self.segment_keys is None) == (self.bbox is None):
            raise ValueError("provide exactly one of segmentKeys or bbox")
        return self


class FreshnessPolicy(ContractModel):
    """Values older than ``max_age_seconds`` come back ``degraded``.

    ``None`` means the server's configured default (900 s proposed, not agreed).
    """

    max_age_seconds: Annotated[int, Field(ge=1)] | None = None


class CitySnapshotQuery(ContractModel):
    """Request for ``getCitySnapshot``."""

    scope: SnapshotScope
    freshness_policy: FreshnessPolicy | None = None
    at_version: Version | None = None


class CitySnapshot(ContractModel):
    """Versioned City State view; absent segments are ``open`` with ``good`` quality."""

    snapshot_version: Version
    generated_at: UtcDatetime
    segments: list[SegmentCondition]
    stale_sources: list[str]
    overall_quality: Quality


class ObservationTarget(ContractModel):
    """Where an observation applies: a segment, a zone, or a lat/lon point."""

    segment_key: SegmentKey | None = None
    zone_key: str | None = None
    lat: Annotated[float, Field(ge=-90, le=90)] | None = None
    lon: Annotated[float, Field(ge=-180, le=180)] | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        has_point = self.lat is not None or self.lon is not None
        if has_point and (self.lat is None or self.lon is None):
            raise ValueError("lat and lon must be given together")
        chosen = [self.segment_key is not None, self.zone_key is not None, has_point]
        if sum(chosen) != 1:
            raise ValueError("provide exactly one of segmentKey, zoneKey or lat/lon")
        return self


class Observation(ContractModel):
    """Raw input to ``ingestObservation``; ``values`` are in source units."""

    observation_id: UUID4
    source_id: str = Field(min_length=1)
    kind: ObservationKind
    target: ObservationTarget
    values: dict[str, Any]
    observed_at: UtcDatetime
    author: str | None = None  # manual reports only (AF5)
    expires_at: UtcDatetime | None = None  # manual reports only (AF5)


class IngestionReceipt(ContractModel):
    """Returned once an observation is persisted; no snapshot when not merged."""

    observation_id: UUID4
    disposition: IngestionDisposition
    snapshot_version: Version | None = None
