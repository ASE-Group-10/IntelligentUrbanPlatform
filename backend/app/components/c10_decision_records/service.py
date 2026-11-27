"""C10 service API, in-process half: recordDecision and recordOutcome (UC10).

Both run on the caller's connection and never commit (internal.md). The read
side (getDecision, explainDecision, getDecisionEvidence) is added separately.

Every call writes one JSON log line (``app.shared.logging``) with
``component=C10`` and the caller's ``correlationId``; recordDecision adds the
record's ``scope`` and ``runId`` (walking skeleton #72, hop 4).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.components.c10_decision_records.contracts import (
    AuditId,
    AuditOutcome,
    DecisionRecord,
    RecordDecisionResponse,
)
from app.components.c10_decision_records.evidence_storage import (
    SupabaseEvidenceStorage,
)
from app.components.c10_decision_records.store import (
    Connection,
    DecisionStore,
    StoredDecision,
    StoredOutcome,
    StoreUnavailableError,
)
from app.shared.c09_c10_common import AuthContext, PrincipalType, RunScope
from app.shared.result import Failure, Ok, Result

log = logging.getLogger(__name__)

EVIDENCE_REF_MISSING = "C10.EVIDENCE_REF_MISSING"
EVIDENCE_CONTENT_MISSING = "C10.EVIDENCE_CONTENT_MISSING"
MISSING_FIELD = "C10.MISSING_FIELD"
PRODUCER_MISMATCH = "C10.PRODUCER_MISMATCH"
IDEMPOTENCY_MISMATCH = "C10.IDEMPOTENCY_MISMATCH"
UNAVAILABLE = "C10.UNAVAILABLE"  # not yet named in the contract; see #60


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


class DecisionRecords:
    """C10 service API (UC10), in-process write operations."""

    def __init__(
        self,
        store: DecisionStore,
        clock: Callable[[], datetime],
        new_id: Callable[[], UUID] = uuid4,
        evidence: SupabaseEvidenceStorage | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id
        self._evidence = evidence

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
            if not result.value.duplicate:
                self._upload_evidence(record, trace)
        else:
            log.warning(
                "recordDecision %s: %s", result.kind, result.message, extra=trace
            )
        return result

    def _upload_evidence(self, record: DecisionRecord, trace: dict[str, str]) -> None:
        """Copy each snapshot to Supabase Storage (#130). Postgres keeps the
        record, so a Storage failure is logged and never fails the decision.
        shortcut: uploads before the caller commits, so a rolled-back decision
        leaves an orphan object; move to an outbox if that matters."""
        if self._evidence is None:
            return
        for snapshot in record.evidence_snapshots:
            data = snapshot.model_dump(mode="json", by_alias=True)
            try:
                key = self._evidence.put_snapshot(record.decision_id, data)
            except StoreUnavailableError as exc:
                log.warning("evidence upload failed: %s", exc, extra=trace)
            else:
                log.info("evidence stored at %s", key, extra=trace)

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
