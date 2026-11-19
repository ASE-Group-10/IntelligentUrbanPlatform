"""C09 Logistics Knowledge contracts (contract doc §7.2–7.8)."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.shared.c09_c10_common import Activity, Model, TimeWindow, VehicleClass


class Area(Model):
    zone_ids: list[str] = Field(min_length=1)  # pending C06: zoneIds vs GeoJSON


class PolicyQuery(Model):
    area: Area
    time_window: TimeWindow
    vehicle_class: VehicleClass
    activity: Activity
    free_text: str | None = Field(default=None, max_length=500)
    max_passages: int = Field(default=5, ge=1, le=20)


class Applicability(Model):
    zone_ids: list[str]
    vehicle_classes: list[VehicleClass]  # empty = all
    activities: list[Activity]  # empty = all


class ConstraintKind(StrEnum):
    VEHICLE_BAN = "VehicleBan"
    LOADING_WINDOW = "LoadingWindow"
    MAX_GROSS_WEIGHT = "MaxGrossWeight"


class TimeOfDay(Model):
    # local Europe/Dublin time, because the policies are written that way
    from_: str = Field(alias="from", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    to: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class Weekday(StrEnum):
    MON = "Mon"
    TUE = "Tue"
    WED = "Wed"
    THU = "Thu"
    FRI = "Fri"
    SAT = "Sat"
    SUN = "Sun"


SegmentKey = Annotated[str, Field(pattern=r"^seg:[1-9][0-9]*$")]
"""C06 road segment key ``seg:<osm_way_id>``."""


class Constraint(Model):
    """A hard rule the planner applies (C05 #21 review, point 1).

    Scope: the rule applies to a route leg or stop inside ``zoneIds``; when
    ``segmentKeys`` is non-empty, only on those C06 segments (empty = the whole
    zones). Fields required per kind: VehicleBan needs ``value`` null;
    LoadingWindow needs ``timeOfDay`` and ``value`` null; MaxGrossWeight needs
    ``value`` = ``{"maxKg": <positive number>}``.
    """

    constraint_id: str
    kind: ConstraintKind
    zone_ids: list[str] = Field(min_length=1)
    segment_keys: list[SegmentKey]  # empty = whole zones
    vehicle_classes: list[VehicleClass]  # empty = all
    time_of_day: TimeOfDay | None
    days_of_week: list[Weekday]  # empty = every day
    value: dict[str, Any] | None
    exemption: str | None

    @model_validator(mode="after")
    def _fields_for_kind(self):
        if self.kind is ConstraintKind.MAX_GROSS_WEIGHT:
            limit = (self.value or {}).get("maxKg")
            if (
                set(self.value or {}) != {"maxKg"}
                or not (isinstance(limit, int | float) and not isinstance(limit, bool))
                or limit <= 0
            ):
                raise ValueError('MaxGrossWeight needs value {"maxKg": <positive>}')
        elif self.value is not None:
            raise ValueError(f"{self.kind} must have value null")
        if self.kind is ConstraintKind.LOADING_WINDOW and self.time_of_day is None:
            raise ValueError("LoadingWindow needs timeOfDay")
        return self


class Passage(Model):
    passage_id: UUID
    content: str = Field(min_length=1)  # verbatim text, never just a reference
    document_id: str
    document_title: str
    document_version: int = Field(ge=1)
    section_id: str
    valid_from: datetime
    valid_to: datetime | None
    applicability: Applicability
    score: float = Field(ge=0, le=1)
    constraints: list[Constraint]


class RetrievalStatus(StrEnum):
    OK = "Ok"
    LOW_CONFIDENCE = "LowConfidence"
    CONFLICT = "Conflict"
    NO_APPLICABLE = "NoApplicable"


class KnowledgeConflict(Model):
    conflict_id: UUID
    passage_ids: list[UUID] = Field(min_length=2)
    reason: str


class RuleCoverage(StrEnum):
    """Whether every applicable hard rule (passage with constraints) is returned.

    ``maxPassages`` never cuts a passage that carries constraints; it only limits
    the rule-free passages. ``Incomplete`` means C09 could not return them all;
    C05 must then not rely on the rules (UC03 DataUnavailable, UC07
    ReviewRequired)."""

    COMPLETE = "Complete"
    INCOMPLETE = "Incomplete"


class _RetrievalFields(Model):
    rule_coverage: RuleCoverage
    conflicts: list[KnowledgeConflict]
    limitations: list[str]
    corpus_version: int = Field(ge=1)
    index_version: str
    embedding_model: str
    retrieved_at: datetime


def _check_status(
    status: RetrievalStatus, passages: list[Passage], conflicts: list[KnowledgeConflict]
) -> None:
    # conflicts are reported if and only if the status says Conflict
    if (status is RetrievalStatus.CONFLICT) != bool(conflicts):
        raise ValueError("status must be Conflict exactly when conflicts is non-empty")
    if status is RetrievalStatus.NO_APPLICABLE and passages:
        raise ValueError("status NoApplicable must have no passages")
    ids = {p.passage_id for p in passages}
    for c in conflicts:
        if not set(c.passage_ids) <= ids:
            raise ValueError("conflict refers to a passage that is not returned")


class EvidenceSnapshot(_RetrievalFields):
    snapshot_id: UUID
    query: PolicyQuery
    retrieval_status: RetrievalStatus
    items: list[Passage]

    @model_validator(mode="after")
    def _consistent(self):
        _check_status(self.retrieval_status, self.items, self.conflicts)
        return self


class KnowledgeResult(_RetrievalFields):
    status: RetrievalStatus
    passages: list[Passage]
    evidence_snapshot: EvidenceSnapshot

    @model_validator(mode="after")
    def _consistent(self):
        _check_status(self.status, self.passages, self.conflicts)
        if self.passages != sorted(
            self.passages, key=lambda p: (-p.score, str(p.passage_id))
        ):
            raise ValueError("passages must be sorted by score desc, then passageId")
        snap = self.evidence_snapshot
        if snap.items != self.passages or snap.retrieval_status != self.status:
            raise ValueError(
                "evidenceSnapshot must exactly copy the returned passages and status"
            )
        # provenance is duplicated for convenience; the snapshot is the record
        for name in _RetrievalFields.model_fields:
            if getattr(snap, name) != getattr(self, name):
                raise ValueError(f"evidenceSnapshot.{name} differs from the result")
        if self.rule_coverage is RuleCoverage.INCOMPLETE and not self.limitations:
            raise ValueError("Incomplete ruleCoverage must be explained in limitations")
        return self


# ---- publishKnowledge ----
class KnowledgeDocument(Model):
    title: str
    format: Literal["markdown"]  # slice: markdown only
    content: str = Field(min_length=1)


class DocumentMetadata(Model):
    document_id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    issuer: str
    applicability: Applicability
    valid_from: datetime
    valid_to: datetime | None
    constraints_by_section: dict[str, list[Constraint]] | None = None


class PublishKnowledgeRequest(Model):
    document: KnowledgeDocument
    metadata: DocumentMetadata
    idempotency_key: str = Field(min_length=1)


class KnowledgePublication(Model):
    document_id: str
    document_version: int = Field(ge=1)
    corpus_version: int = Field(ge=1)
    index_version: str
    chunk_count: int = Field(ge=1)
    activated_at: datetime
    duplicate: bool


# ---- notifyKnowledgeConflict (C09-internal) ----
class ConflictDetails(Model):
    conflict_id: UUID
    passage_ids: list[UUID] = Field(min_length=2)
    document_ids: list[str]
    reason: str


class WarningReceipt(Model):
    warning_id: UUID
    duplicate: bool
