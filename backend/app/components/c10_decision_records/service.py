"""C10 service API (UC10): record decisions and outcomes, read decisions back.

Every call runs on the caller's connection and never commits (internal.md).
explainDecision is added separately; ``router.py`` serves the reads over HTTP.

Every call writes one JSON log line (``app.shared.logging``) with
``component=C10`` and the caller's ``correlationId``; recordDecision adds the
record's ``scope`` and ``runId`` (walking skeleton #72, hop 4).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.components.c10_decision_records.contracts import (
    AuditId,
    AuditOutcome,
    DecisionEvidence,
    DecisionIdSelector,
    DecisionRecord,
    DecisionRecordView,
    EvidenceStatus,
    EvidenceStatusEntry,
    RecordDecisionResponse,
    SubjectSelector,
)
from app.components.c10_decision_records.store import (
    Connection,
    DecisionStore,
    StoredDecision,
    StoredOutcome,
    StoreUnavailableError,
    recorded_in,
)
from app.shared.c09_c10_common import (
    AuthContext,
    PrincipalType,
    RunScope,
)
from app.shared.result import Failure, Ok, Result

log = logging.getLogger(__name__)

EVIDENCE_REF_MISSING = "C10.EVIDENCE_REF_MISSING"
EVIDENCE_CONTENT_MISSING = "C10.EVIDENCE_CONTENT_MISSING"
MISSING_FIELD = "C10.MISSING_FIELD"
PRODUCER_MISMATCH = "C10.PRODUCER_MISMATCH"
IDEMPOTENCY_MISMATCH = "C10.IDEMPOTENCY_MISMATCH"
DECISION_NOT_FOUND = "C10.DECISION_NOT_FOUND"
DECISION_AMBIGUOUS = "C10.DECISION_AMBIGUOUS"
UNAVAILABLE = "C10.UNAVAILABLE"  # not yet named in the contract; see #60
FORBIDDEN = "C10.FORBIDDEN"  # likewise
EVIDENCE_NOT_FOUND = "C10.EVIDENCE_NOT_FOUND"  # likewise

READ_ROLES = {"LogisticsOperator", "Analyst", "Service:C12"}


@dataclass(frozen=True)
class AmbiguousDecision(Failure):
    """``C10.DECISION_AMBIGUOUS`` with the decisions the caller can choose from."""

    candidates: tuple[tuple[UUID, int | None], ...] = ()
    """``(decisionId, subjectVersion)``, in the order they were recorded."""


def parse_decision_record(data: dict[str, Any]) -> Result[DecisionRecord]:
    """Validate a camelCase DecisionRecord payload and map validation errors to
    the contract's failure codes (internal.md, recordDecision failures)."""
    try:
        return Ok(DecisionRecord.model_validate(data))
    except ValidationError as exc:
        errors = exc.errors()
        if any(EVIDENCE_REF_MISSING in e["msg"] for e in errors):
            code = EVIDENCE_REF_MISSING
        elif any(_is_evidence_content(e["loc"]) for e in errors):
            code = EVIDENCE_CONTENT_MISSING
        else:
            code = MISSING_FIELD
        return Failure("Invalid", f"{code}: {_summary(errors)}")


def _is_evidence_content(loc: tuple[int | str, ...]) -> bool:
    return bool(loc) and loc[0] == "evidenceSnapshots" and loc[-1] == "content"


def _summary(errors: list[Any]) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'record'}: {e['msg']}"
        for e in errors[:5]
    )


def _hash(record: DecisionRecord) -> str:
    """Fingerprint of the request body for idempotency. Object keys are sorted
    so a retry that rebuilds a free-form object (``selectedOption.details``)
    in another key order is still the same request; list order is kept."""
    canonical = json.dumps(
        record.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _trace(ctx: AuthContext, run_scope: RunScope | None = None) -> dict[str, str]:
    """``extra`` fields for one C10 log line. ``scope`` uses the event
    envelope's lower-case values (``live`` / ``simulation``)."""
    fields = {"component": "C10", "correlationId": str(ctx.correlation_id)}
    if run_scope is not None:
        fields["scope"] = run_scope.mode.value.lower()
        if run_scope.run_id is not None:
            fields["runId"] = str(run_scope.run_id)
    return fields


def _may_read(ctx: AuthContext) -> bool:
    """Records carry no provider or zone yet, so a caller limited to some of them
    cannot be shown to own a decision and is refused (open question in #60)."""
    limited = ctx.scope is not None and (
        ctx.scope.provider_ids is not None or ctx.scope.zone_ids is not None
    )
    return not limited and not READ_ROLES.isdisjoint(ctx.roles)


def _view(stored: StoredDecision) -> DecisionRecordView:
    record = stored.record
    saved = {p.passage_id for s in record.evidence_snapshots for p in s.items}
    cited = dict.fromkeys(  # unique, in the order they are first cited
        ref
        for source in [*record.reasons, *record.considered_alternatives]
        for ref in source.evidence_ref_ids
    )
    return DecisionRecordView(
        **record.model_dump(),
        recorded_at=stored.recorded_at,
        evidence_status=[
            EvidenceStatusEntry(
                ref_id=ref,
                status=(
                    EvidenceStatus.STORED if ref in saved else EvidenceStatus.MISSING
                ),
            )
            for ref in cited
        ],
    )


def _evidence(record: DecisionRecord, reference_id: str) -> Result[DecisionEvidence]:
    for snapshot in record.evidence_snapshots:
        for passage in snapshot.items:
            if str(passage.passage_id) == reference_id.lower():
                return Ok(
                    DecisionEvidence(
                        decision_id=record.decision_id,
                        reference_id=str(passage.passage_id),
                        kind="Evidence",
                        status=EvidenceStatus.STORED,
                        evidence=passage,
                        alternative=None,
                    )
                )
    for alternative in record.considered_alternatives:
        if alternative.option_id == reference_id:
            return Ok(
                DecisionEvidence(
                    decision_id=record.decision_id,
                    reference_id=reference_id,
                    kind="Alternative",
                    status=EvidenceStatus.STORED,
                    evidence=None,
                    alternative=alternative,
                )
            )
    return Failure(
        "NotFound", f"{EVIDENCE_NOT_FOUND}: not part of this decision's record"
    )


def _forbidden() -> Failure:
    return Failure("Forbidden", f"{FORBIDDEN}: the caller may not read decisions")


def _not_found() -> Failure:
    return Failure("NotFound", f"{DECISION_NOT_FOUND}: no such decision")


def _log_read[T](operation: str, ctx: AuthContext, result: Result[T]) -> Result[T]:
    if isinstance(result, Ok):
        log.info("%s Ok", operation, extra=_trace(ctx))
    else:
        log.warning(
            "%s %s: %s", operation, result.kind, result.message, extra=_trace(ctx)
        )
    return result


class DecisionRecords:
    """C10 service API (UC10), in process."""

    def __init__(
        self,
        store: DecisionStore,
        clock: Callable[[], datetime],
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id

    # ---- recordDecision (internal.md) ----
    def record_decision(
        self, conn: Connection, ctx: AuthContext, record: DecisionRecord
    ) -> Result[RecordDecisionResponse]:
        """Store one decision attempt and its evidence on the caller's
        connection. Insert-only and idempotent on ``idempotencyKey``."""
        result = self._record_decision(conn, ctx, record)
        trace = _trace(ctx, record.run_scope)
        if isinstance(result, Ok):
            log.info(
                "recordDecision %s %s %s for %s %s%s (pending caller commit)",
                record.decision_type.value,
                record.attempt_outcome.value,
                result.value.decision_id,
                record.subject.type.value,
                record.subject.id,
                ", duplicate" if result.value.duplicate else "",
                extra=trace,
            )
        else:
            log.warning(
                "recordDecision %s: %s", result.kind, result.message, extra=trace
            )
        return result

    def _record_decision(
        self, conn: Connection, ctx: AuthContext, record: DecisionRecord
    ) -> Result[RecordDecisionResponse]:
        if (
            ctx.principal_type is not PrincipalType.SERVICE
            or f"Service:{record.producer}" not in ctx.roles
        ):
            return Failure(
                "Forbidden",
                f"{PRODUCER_MISMATCH}: producer {record.producer} is not the caller",
            )
        request_hash = _hash(record)
        try:
            existing = self._existing(conn, record, request_hash)
            if existing is not None:
                return existing
            recorded_at = self._clock()
            inserted = self._store.insert_decision(
                conn, StoredDecision(record, request_hash, recorded_at)
            )
            if not inserted:
                # a concurrent transaction recorded the same key or ID first
                return self._existing(conn, record, request_hash) or Failure(
                    "Conflict", f"{IDEMPOTENCY_MISMATCH}: recorded concurrently"
                )
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"{UNAVAILABLE}: {exc}")
        return Ok(
            RecordDecisionResponse(
                decision_id=record.decision_id,
                recorded_at=recorded_at,
                duplicate=False,
            )
        )

    def _existing(
        self, conn: Connection, record: DecisionRecord, request_hash: str
    ) -> Result[RecordDecisionResponse] | None:
        """The outcome for a record whose key or ID is already stored, or
        ``None`` when neither is."""
        previous = self._store.find_by_idempotency_key(conn, record.idempotency_key)
        if previous is not None:
            if previous.request_hash != request_hash:
                return Failure(
                    "Conflict",
                    f"{IDEMPOTENCY_MISMATCH}: key already used with another body",
                )
            return Ok(
                RecordDecisionResponse(
                    decision_id=previous.record.decision_id,
                    recorded_at=previous.recorded_at,
                    duplicate=True,
                )
            )
        if self._store.find_by_decision_id(conn, record.decision_id) is not None:
            return Failure(
                "Conflict",
                f"{IDEMPOTENCY_MISMATCH}: decisionId already recorded "
                "under another idempotency key",
            )
        return None

    # ---- recordOutcome (internal.md) ----
    def record_outcome(
        self, conn: Connection, ctx: AuthContext, outcome: AuditOutcome
    ) -> Result[AuditId]:
        """Append one audit entry for a protected operation, allowed or not.
        For a rejected operation the caller passes a short transaction of its
        own, so the entry survives the rollback of the rejected work."""
        audit_id = self._new_id()
        try:
            self._store.insert_outcome(
                conn,
                StoredOutcome(
                    audit_id=audit_id,
                    outcome=outcome,
                    principal_id=ctx.principal_id,
                    correlation_id=ctx.correlation_id,
                    recorded_at=self._clock(),
                ),
            )
        except StoreUnavailableError as exc:
            failure = Failure("Unavailable", f"{UNAVAILABLE}: {exc}")
            log.warning("recordOutcome %s", failure.message, extra=_trace(ctx))
            return failure
        log.info(
            "recordOutcome %s %s %s (pending caller commit)",
            outcome.operation,
            outcome.outcome.value,
            audit_id,
            extra=_trace(ctx),
        )
        return Ok(AuditId(audit_id=audit_id))

    # ---- getDecision / findDecision / getDecisionEvidence (openapi.yaml) ----
    def get_decision(
        self, conn: Connection, ctx: AuthContext, selector: DecisionIdSelector
    ) -> Result[DecisionRecordView]:
        """The record as stored. Another run's decision is NotFound."""
        found = self._find(conn, ctx, selector)
        result = Ok(_view(found.value)) if isinstance(found, Ok) else found
        return _log_read("getDecision", ctx, result)

    def find_decision(
        self, conn: Connection, ctx: AuthContext, selector: SubjectSelector
    ) -> Result[DecisionRecordView]:
        """The only decision about a subject; several are a Conflict."""
        found = self._find_for_subject(conn, ctx, selector)
        result = Ok(_view(found.value)) if isinstance(found, Ok) else found
        return _log_read("findDecision", ctx, result)

    def get_decision_evidence(
        self,
        conn: Connection,
        ctx: AuthContext,
        selector: DecisionIdSelector,
        reference_id: str,
    ) -> Result[DecisionEvidence]:
        """One passage or alternative as saved at decision time. Never calls C09."""
        found = self._find(conn, ctx, selector)
        result = (
            _evidence(found.value.record, reference_id)
            if isinstance(found, Ok)
            else found
        )
        return _log_read("getDecisionEvidence", ctx, result)

    def _find(
        self, conn: Connection, ctx: AuthContext, selector: DecisionIdSelector
    ) -> Result[StoredDecision]:
        if not _may_read(ctx):
            return _forbidden()
        try:
            stored = self._store.find_by_decision_id(conn, selector.decision_id)
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"{UNAVAILABLE}: {exc}")
        if stored is None or not recorded_in(stored.record, selector.run_id):
            return _not_found()
        return Ok(stored)

    def _find_for_subject(
        self, conn: Connection, ctx: AuthContext, selector: SubjectSelector
    ) -> Result[StoredDecision]:
        if not _may_read(ctx):
            return _forbidden()
        try:
            matches = self._store.find_by_subject(conn, selector)
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"{UNAVAILABLE}: {exc}")
        if not matches:
            return _not_found()
        if len(matches) > 1:
            return AmbiguousDecision(
                "Conflict",
                f"{DECISION_AMBIGUOUS}: more than one decision matches this "
                "subject; specify subjectVersion or decisionId",
                tuple(
                    (s.record.decision_id, s.record.subject.version) for s in matches
                ),
            )
        return Ok(matches[0])
