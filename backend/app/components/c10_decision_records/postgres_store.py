"""PostgreSQL implementation of ``DecisionStore`` (schema ``c10_decision_records``).

Tables come from ``database/migrations/20261121100000_create_c10_decision_records``.

recordDecision and recordOutcome run inside the caller's transaction, so every
method here executes on the ``psycopg.Connection`` it is given and never
commits, rolls back or closes it. The caller's connection is expected to use
psycopg's default READ COMMITTED isolation: a concurrent insert of the same
idempotency key then waits for the other transaction, and the record it
committed is visible to the follow-up read.
"""

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.components.c10_decision_records.contracts import DecisionRecord, SubjectType
from app.components.c10_decision_records.store import (
    StoredDecision,
    StoredOutcome,
    StoreUnavailableError,
)

_SELECT_DECISION = (
    "select record, request_hash, recorded_at from c10_decision_records.decisions"
)


def _stored(row: tuple) -> StoredDecision:
    record, request_hash, recorded_at = row
    try:
        parsed = DecisionRecord.model_validate(record)
    except ValidationError as error:
        # for example a record written under an older contract version
        raise StoreUnavailableError("a stored decision cannot be read") from error
    return StoredDecision(parsed, request_hash, recorded_at)


class PostgresDecisionStore:
    """``DecisionStore`` backed by PostgreSQL (local Docker or Supabase)."""

    def find_by_idempotency_key(
        self, conn: psycopg.Connection, idempotency_key: str
    ) -> StoredDecision | None:
        row = self._fetch(
            conn, f"{_SELECT_DECISION} where idempotency_key = %s", (idempotency_key,)
        )
        return _stored(row) if row else None

    def find_by_decision_id(
        self, conn: psycopg.Connection, decision_id: UUID
    ) -> StoredDecision | None:
        row = self._fetch(
            conn, f"{_SELECT_DECISION} where decision_id = %s", (decision_id,)
        )
        return _stored(row) if row else None

    def find_by_subject(
        self, conn: psycopg.Connection, subject_type: SubjectType, subject_id: UUID
    ) -> list[StoredDecision]:
        try:
            rows = conn.execute(
                f"{_SELECT_DECISION}"
                " where subject_type = %s and subject_id = %s order by seq",
                (subject_type.value, subject_id),
            ).fetchall()
        except psycopg.OperationalError as error:
            raise StoreUnavailableError(f"decision store failed: {error}") from error
        return [_stored(row) for row in rows]

    def insert_decision(
        self, conn: psycopg.Connection, decision: StoredDecision
    ) -> bool:
        record = decision.record
        row = self._fetch(
            conn,
            "insert into c10_decision_records.decisions"
            " (decision_id, idempotency_key, request_hash, producer, decision_type,"
            "  subject_type, subject_id, subject_version, attempt_outcome,"
            "  run_mode, run_id, corpus_version, decided_at, recorded_at, record)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " on conflict do nothing returning decision_id",
            (
                record.decision_id,
                record.idempotency_key,
                decision.request_hash,
                record.producer,
                record.decision_type.value,
                record.subject.type.value,
                record.subject.id,
                record.subject.version,
                record.attempt_outcome.value,
                record.run_scope.mode.value,
                record.run_scope.run_id,
                record.input_versions.corpus_version,
                record.decided_at,
                decision.recorded_at,
                Jsonb(record.model_dump(mode="json", by_alias=True)),
            ),
        )
        return row is not None

    def insert_outcome(self, conn: psycopg.Connection, outcome: StoredOutcome) -> None:
        entry = outcome.outcome
        self._fetch(
            conn,
            "insert into c10_decision_records.audit_outcomes"
            " (audit_id, operation, outcome, principal_id, correlation_id,"
            "  occurred_at, recorded_at, entry)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s) returning audit_id",
            (
                outcome.audit_id,
                entry.operation,
                entry.outcome.value,
                outcome.principal_id,
                outcome.correlation_id,
                entry.occurred_at,
                outcome.recorded_at,
                Jsonb(entry.model_dump(mode="json", by_alias=True)),
            ),
        )

    @staticmethod
    def _fetch(conn: psycopg.Connection, query: str, params: tuple) -> tuple | None:
        try:
            return conn.execute(query, params).fetchone()
        except psycopg.OperationalError as error:
            raise StoreUnavailableError(f"decision store failed: {error}") from error
