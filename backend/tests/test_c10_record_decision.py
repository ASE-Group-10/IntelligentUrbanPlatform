"""UC10 recordDecision / recordOutcome acceptance examples (#60).

Behavioural tests run once per store (in-memory and PostgreSQL, like C06's
``new_store``); Postgres runs only when ``TEST_DATABASE_URL`` is set. The caller's
transaction is an ``InMemoryTransaction`` or a real ``psycopg.Connection``; both
offer ``commit()`` and ``rollback()``.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from app.components.c10_decision_records.contracts import (
    AttemptOutcome,
    AuditOutcome,
    DecisionRecord,
)
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import (
    DecisionRecords,
    parse_decision_record,
)
from app.components.c10_decision_records.store import (
    InMemoryDecisionStore,
    StoredDecision,
    StoredOutcome,
    TransactionClosedError,
)
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure, Ok

EXAMPLES = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "components"
    / "c10-decision-records"
    / "examples"
)
NOW = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 11, 19, 9, 45, tzinfo=UTC)
AUDIT_ID = UUID("11111111-2222-4333-8444-555555555555")
C10_TABLES = ("c10_decision_records.decisions", "c10_decision_records.audit_outcomes")


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _record(name: str = "record-decision-request.json", **changes) -> DecisionRecord:
    data = _example(name)
    data.update(changes)
    return DecisionRecord.model_validate(data)


def _outcome() -> AuditOutcome:
    return AuditOutcome.model_validate(_example("record-outcome-request.json"))


def _ctx(principal_type: str, *roles: str) -> AuthContext:
    return AuthContext(
        principal_id="service:c05-planner",
        principal_type=principal_type,
        roles=list(roles),
        correlation_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    )


C05 = _ctx("Service", "Service:C05")
C08 = _ctx("Service", "Service:C08")


class Clock:
    """Controlled clock: returns ``now`` and can be moved forward."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


# ---- the two stores behind one test interface ----


class MemoryBackend:
    def __init__(self) -> None:
        self.store = InMemoryDecisionStore()

    def begin(self):
        return self.store.begin()

    def decisions(self) -> dict[UUID, StoredDecision]:
        return dict(self.store.decisions)

    def outcomes(self) -> list[StoredOutcome]:
        return list(self.store.outcomes)

    def close(self) -> None:
        pass


class PostgresBackend:
    """Each ``begin()`` opens a new connection, as a calling component would."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.store = PostgresDecisionStore()
        self._connections: list[psycopg.Connection] = []

    def begin(self) -> psycopg.Connection:
        conn = psycopg.connect(self.url)
        self._connections.append(conn)
        return conn

    def decisions(self) -> dict[UUID, StoredDecision]:
        """Committed decisions, read on a separate connection."""
        with psycopg.connect(self.url) as conn:
            rows = conn.execute(
                "select record, request_hash, recorded_at"
                " from c10_decision_records.decisions order by seq"
            ).fetchall()
        stored = [
            StoredDecision(DecisionRecord.model_validate(r), h, t) for r, h, t in rows
        ]
        return {s.record.decision_id: s for s in stored}

    def outcomes(self) -> list[StoredOutcome]:
        with psycopg.connect(self.url) as conn:
            rows = conn.execute(
                "select audit_id, entry, principal_id, correlation_id, recorded_at"
                " from c10_decision_records.audit_outcomes order by seq"
            ).fetchall()
        return [
            StoredOutcome(a, AuditOutcome.model_validate(e), p, c, t)
            for a, e, p, c, t in rows
        ]

    def close(self) -> None:
        for conn in self._connections:
            conn.close()


@pytest.fixture
def c10_database(test_database_url: str) -> str:
    """Empty C10 tables before each Postgres-backed test."""
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(f"truncate {', '.join(C10_TABLES)}")
    return test_database_url


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    if request.param == "memory":
        b = MemoryBackend()
    else:
        b = PostgresBackend(request.getfixturevalue("c10_database"))
    yield b
    b.close()


@pytest.fixture
def pg(c10_database):
    """Postgres only, for behaviour the in-memory double does not model."""
    b = PostgresBackend(c10_database)
    yield b
    b.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _service(backend, clock) -> DecisionRecords:
    return DecisionRecords(backend.store, clock=clock, new_id=lambda: AUDIT_ID)


@pytest.fixture
def service(backend, clock) -> DecisionRecords:
    return _service(backend, clock)


def _record_and_commit(service, backend, ctx, record):
    tx = backend.begin()
    result = service.record_decision(tx, ctx, record)
    tx.commit()
    return result


# ---- recordDecision: stored with the caller's transaction ----


def test_selected_decision_is_stored_when_the_caller_commits(service, backend):
    record = _record()
    tx = backend.begin()

    result = service.record_decision(tx, C05, record)

    assert isinstance(result, Ok)
    assert result.value.decision_id == record.decision_id
    assert result.value.recorded_at == NOW
    assert result.value.duplicate is False
    assert backend.decisions() == {}  # C10 never commits on its own
    tx.commit()
    stored = backend.decisions()[record.decision_id]
    assert stored.record == record  # evidence snapshots kept exactly as received
    assert stored.recorded_at == NOW


def test_nothing_remains_when_the_caller_rolls_back(service, backend):
    tx = backend.begin()
    assert isinstance(service.record_decision(tx, C05, _record()), Ok)

    tx.rollback()

    assert backend.decisions() == {}


# ---- recordDecision: idempotency ----


def test_same_key_and_body_returns_the_original_decision(service, backend, clock):
    record = _record()
    _record_and_commit(service, backend, C05, record)
    clock.now = LATER

    again = _record_and_commit(service, backend, C05, record)

    assert isinstance(again, Ok)
    assert again.value.duplicate is True
    assert again.value.decision_id == record.decision_id
    assert again.value.recorded_at == NOW  # the original time, not the retry time
    assert len(backend.decisions()) == 1


def test_duplicate_is_detected_inside_the_same_transaction(service, backend):
    tx = backend.begin()
    service.record_decision(tx, C05, _record())

    again = service.record_decision(tx, C05, _record())
    tx.commit()

    assert isinstance(again, Ok) and again.value.duplicate is True
    assert len(backend.decisions()) == 1


def test_same_key_with_another_body_is_a_conflict(service, backend):
    _record_and_commit(service, backend, C05, _record())
    changed = _record(limitations=["recomputed"])

    result = _record_and_commit(service, backend, C05, changed)

    assert isinstance(result, Failure)
    assert result.kind == "Conflict"
    assert result.message.startswith("C10.IDEMPOTENCY_MISMATCH")


def test_decision_id_reused_under_another_key_is_a_conflict(service, backend):
    _record_and_commit(service, backend, C05, _record())
    reused = _record(idempotencyKey="c05:plan:other:v1")

    result = _record_and_commit(service, backend, C05, reused)

    assert isinstance(result, Failure) and result.kind == "Conflict"
    assert len(backend.decisions()) == 1


# ---- recordDecision: attempts that selected nothing (UC03 AF2) ----


def test_failed_attempt_is_stored_without_option_or_version(service, backend):
    record = _record("record-decision-data-unavailable-request.json")

    result = _record_and_commit(service, backend, C05, record)

    assert isinstance(result, Ok)
    stored = backend.decisions()[record.decision_id].record
    assert stored.attempt_outcome is AttemptOutcome.DATA_UNAVAILABLE
    assert stored.selected_option is None
    assert stored.subject.version is None


# ---- recordDecision: producer must be the calling service ----


@pytest.mark.parametrize(
    "ctx",
    [
        C08,  # another service recording C05's decision
        _ctx("User", "Service:C05"),  # a user, even with a service-like role
        _ctx("Service"),  # a service with no roles
    ],
)
def test_producer_must_match_the_calling_service(service, backend, ctx):
    result = _record_and_commit(service, backend, ctx, _record())

    assert isinstance(result, Failure)
    assert result.kind == "Forbidden"
    assert result.message.startswith("C10.PRODUCER_MISMATCH")
    assert backend.decisions() == {}


# ---- recordOutcome ----


def test_outcome_is_stored_with_principal_and_correlation_from_context(
    service, backend
):
    ctx = _ctx("User", "LogisticsOperator")
    tx = backend.begin()

    result = service.record_outcome(tx, ctx, _outcome())
    tx.commit()

    assert isinstance(result, Ok) and result.value.audit_id == AUDIT_ID
    [entry] = backend.outcomes()
    assert entry.outcome == _outcome()
    assert entry.principal_id == ctx.principal_id
    assert entry.correlation_id == ctx.correlation_id
    assert entry.recorded_at == NOW


def test_outcome_rolls_back_with_the_caller(service, backend):
    tx = backend.begin()
    service.record_outcome(tx, C05, _outcome())

    tx.rollback()

    assert backend.outcomes() == []


# ---- in-memory double only ----


def test_memory_unavailable_store_reports_unavailable(clock):
    backend = MemoryBackend()
    service = _service(backend, clock)
    tx = backend.begin()
    backend.store.available = False

    assert service.record_decision(tx, C05, _record()).kind == "Unavailable"
    assert service.record_outcome(tx, C05, _outcome()).kind == "Unavailable"


def test_memory_closed_transaction_cannot_be_reused(clock):
    backend = MemoryBackend()
    service = _service(backend, clock)
    tx = backend.begin()
    tx.commit()

    with pytest.raises(TransactionClosedError):
        service.record_decision(tx, C05, _record())


# ---- PostgreSQL only ----


def test_postgres_closed_connection_reports_unavailable(pg, clock):
    service = _service(pg, clock)
    conn = pg.begin()
    conn.close()

    assert service.record_decision(conn, C05, _record()).kind == "Unavailable"
    assert service.record_outcome(conn, C05, _outcome()).kind == "Unavailable"


def test_postgres_concurrent_retry_waits_and_returns_the_first(pg, clock):
    """Two callers record the same decision at once (e.g. a retried command).
    The second waits for the first to commit, then gets the original record."""
    service = _service(pg, clock)
    record = _record()
    first = pg.begin()
    assert service.record_decision(first, C05, record).value.duplicate is False

    second = pg.begin()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.record_decision, second, C05, record)
        time.sleep(0.5)
        assert not pending.done()  # blocked on the first transaction's row
        first.commit()
        result = pending.result(timeout=10)
    second.commit()

    assert isinstance(result, Ok) and result.value.duplicate is True
    assert len(pg.decisions()) == 1


def test_postgres_stored_rows_cannot_be_changed(pg, clock):
    service = _service(pg, clock)
    _record_and_commit(service, pg, C05, _record())
    tx = pg.begin()
    service.record_outcome(tx, C05, _outcome())
    tx.commit()

    with psycopg.connect(pg.url, autocommit=True) as conn:
        for table in C10_TABLES:
            with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
                conn.execute(f"update {table} set recorded_at = now()")
            with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
                conn.execute(f"delete from {table}")
    assert len(pg.decisions()) == 1 and len(pg.outcomes()) == 1


# ---- parse_decision_record: contract failure codes ----


def test_valid_payload_parses():
    result = parse_decision_record(_example("record-decision-request.json"))
    assert isinstance(result, Ok)


def test_reason_citing_unknown_evidence_is_evidence_ref_missing():
    data = _example("record-decision-request.json")
    data["reasons"][0]["evidenceRefIds"].append(str(uuid4()))

    result = parse_decision_record(data)

    assert isinstance(result, Failure) and result.kind == "Invalid"
    assert result.message.startswith("C10.EVIDENCE_REF_MISSING")


def test_evidence_without_content_is_evidence_content_missing():
    data = _example("record-decision-request.json")
    del data["evidenceSnapshots"][0]["items"][0]["content"]

    result = parse_decision_record(data)

    assert isinstance(result, Failure) and result.kind == "Invalid"
    assert result.message.startswith("C10.EVIDENCE_CONTENT_MISSING")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("idempotencyKey"),
        lambda d: d.update(decisionType="Guess"),
        lambda d: d.update(producer="C04"),
    ],
)
def test_missing_field_or_bad_enum_is_missing_field(mutate):
    data = _example("record-decision-request.json")
    mutate(data)

    result = parse_decision_record(data)

    assert isinstance(result, Failure) and result.kind == "Invalid"
    assert result.message.startswith("C10.MISSING_FIELD")
