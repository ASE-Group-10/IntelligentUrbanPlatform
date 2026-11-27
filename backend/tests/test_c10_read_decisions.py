"""UC10 getDecision / findDecision / getDecisionEvidence acceptance examples (#60).

Each behaviour runs on the in-memory and the PostgreSQL store; the latter only
when ``TEST_DATABASE_URL`` is set.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import yaml
from psycopg.types.json import Jsonb

from app.components.c10_decision_records.contracts import (
    DecisionIdSelector,
    DecisionRecord,
    SubjectSelector,
)
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import (
    AmbiguousDecision,
    DecisionRecords,
)
from app.components.c10_decision_records.store import InMemoryDecisionStore
from app.shared.c09_c10_common import AuthContext, AuthScope
from app.shared.result import Failure, Ok

CONTRACT = (
    Path(__file__).resolve().parents[2] / "contracts/components/c10-decision-records"
)
RESPONSES = yaml.safe_load((CONTRACT / "openapi.yaml").read_text(encoding="utf-8"))[
    "components"
]["examples"]
RECORDED_AT = datetime(2026, 11, 20, 6, 55, 4, tzinfo=UTC)
RUN = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
OTHER_RUN = UUID("d4a2b3c5-6e7f-4081-9bac-1d2e3f4a5b6c")
PASSAGE = "5b0c2f1e-8a4d-4c2b-9e61-3f7d2a9c1b01"
SIMULATED = {"mode": "Simulation", "runId": str(RUN)}
LIVE = {"mode": "Live", "runId": None}


def _ctx(*roles: str, kind: str = "User", scope: AuthScope | None = None):
    return AuthContext(
        principal_id="user:1",
        principal_type=kind,
        roles=list(roles),
        scope=scope,
        correlation_id=uuid4(),
    )


C05 = _ctx("Service:C05", kind="Service")
OPERATOR = _ctx("LogisticsOperator")
REPLAY = _ctx("Service:C12", kind="Service")


def _record(**changes) -> DecisionRecord:
    path = CONTRACT / "examples/record-decision-request.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return DecisionRecord.model_validate(data | changes)


def _another(version: int = 1, **changes) -> DecisionRecord:
    """A further decision about the example's plan."""
    subject = _record().subject.model_dump(mode="json") | {"version": version}
    return _record(
        decisionId=str(uuid4()),
        idempotencyKey=str(uuid4()),
        subject=subject,
        **changes,
    )


def _by_id(record: DecisionRecord, run_id: UUID | None = None) -> DecisionIdSelector:
    return DecisionIdSelector(decision_id=record.decision_id, run_id=run_id)


def _by_subject(record: DecisionRecord, **changes) -> SubjectSelector:
    return SubjectSelector(
        subject_type=record.subject.type, subject_id=record.subject.id, **changes
    )


@pytest.fixture(params=["memory", "postgres"])
def opened(request):
    """``(store, connection)`` for one transaction, on each store."""
    if request.param == "memory":
        store = InMemoryDecisionStore()
        yield store, store.begin()
        return
    url = request.getfixturevalue("test_database_url")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("truncate c10_decision_records.decisions")
    conn = psycopg.connect(url)
    yield PostgresDecisionStore(), conn
    conn.close()


@pytest.fixture
def conn(opened):
    return opened[1]


@pytest.fixture
def service(opened) -> DecisionRecords:
    return DecisionRecords(opened[0], clock=lambda: RECORDED_AT)


def _save(service, conn, *records: DecisionRecord) -> None:
    for record in records:
        assert isinstance(service.record_decision(conn, C05, record), Ok)


def _read_all(service, conn, ctx, record, run_id=None) -> list:
    """The result of each read operation for the same decision."""
    by_id = _by_id(record, run_id)
    return [
        service.get_decision(conn, ctx, by_id),
        service.find_decision(conn, ctx, _by_subject(record, run_id=run_id)),
        service.get_decision_evidence(conn, ctx, by_id, PASSAGE),
    ]


def _assert_failure(result, kind: str, code: str) -> None:
    assert isinstance(result, Failure)
    assert result.kind == kind
    assert result.message.startswith(f"{code}:")


# ---- getDecision ----


def test_decision_matches_the_contract_example(service, conn):
    record = _record()
    _save(service, conn, record)

    result = service.get_decision(conn, OPERATOR, _by_id(record))

    assert isinstance(result, Ok)
    body = result.value.model_dump(mode="json", by_alias=True)
    assert body == RESPONSES["C10GetDecisionResponse"]["value"]


def test_unknown_decision_is_not_found(service, conn):
    result = service.get_decision(conn, OPERATOR, _by_id(_record()))

    _assert_failure(result, "NotFound", "C10.DECISION_NOT_FOUND")


# ---- run isolation (C12 replay) ----


def test_replay_reads_decisions_of_its_own_run(service, conn):
    record = _record(runScope=SIMULATED)
    _save(service, conn, record)

    for result in _read_all(service, conn, REPLAY, record, RUN):
        assert isinstance(result, Ok)


@pytest.mark.parametrize(
    ("run_scope", "run_id"),
    [(SIMULATED, OTHER_RUN), (SIMULATED, None), (LIVE, RUN)],
    ids=["another-run", "run-id-left-out", "live-decision"],
)
def test_decision_outside_the_requested_run_is_not_found(
    service, conn, run_scope, run_id
):
    record = _record(runScope=run_scope)
    _save(service, conn, record)

    for result in _read_all(service, conn, REPLAY, record, run_id):
        _assert_failure(result, "NotFound", "C10.DECISION_NOT_FOUND")


# ---- who may read ----


@pytest.mark.parametrize(
    "ctx",
    [
        _ctx("Driver"),
        _ctx(),
        _ctx("LogisticsOperator", scope=AuthScope(provider_ids=["provider-b"])),
        _ctx("Analyst", scope=AuthScope(zone_ids=["dublin-D2"])),
    ],
    ids=["driver", "no-role", "provider-limited", "zone-limited"],
)
def test_callers_that_cannot_be_authorised_are_refused(service, conn, ctx):
    record = _record()
    _save(service, conn, record)

    for result in _read_all(service, conn, ctx, record):
        _assert_failure(result, "Forbidden", "C10.FORBIDDEN")


# ---- findDecision ----


def test_single_decision_is_found_by_subject(service, conn):
    record = _record()
    _save(service, conn, record)

    result = service.find_decision(conn, OPERATOR, _by_subject(record))

    assert isinstance(result, Ok)
    assert result.value.decision_id == record.decision_id


def test_several_versions_are_ambiguous_until_one_is_named(service, conn):
    first, second = _record(), _another(version=2)
    _save(service, conn, first, second)

    result = service.find_decision(conn, OPERATOR, _by_subject(first))
    chosen = service.find_decision(
        conn, OPERATOR, _by_subject(first, subject_version=2)
    )

    _assert_failure(result, "Conflict", "C10.DECISION_AMBIGUOUS")
    assert isinstance(result, AmbiguousDecision)
    assert result.candidates == ((first.decision_id, 1), (second.decision_id, 2))
    assert isinstance(chosen, Ok)
    assert chosen.value.decision_id == second.decision_id


def test_several_decisions_about_one_version_stay_ambiguous(service, conn):
    first = _record()
    _save(service, conn, first, _another(version=1))

    result = service.find_decision(
        conn, OPERATOR, _by_subject(first, subject_version=1)
    )

    _assert_failure(result, "Conflict", "C10.DECISION_AMBIGUOUS")


def test_another_runs_decision_is_not_a_candidate(service, conn):
    live = _record()
    _save(service, conn, live, _another(version=2, runScope=SIMULATED))

    result = service.find_decision(conn, OPERATOR, _by_subject(live))

    assert isinstance(result, Ok)
    assert result.value.decision_id == live.decision_id


# ---- getDecisionEvidence ----


def test_stored_passage_matches_the_contract_example(service, conn):
    record = _record()
    _save(service, conn, record)

    result = service.get_decision_evidence(conn, OPERATOR, _by_id(record), PASSAGE)

    assert isinstance(result, Ok)
    body = result.value.model_dump(mode="json", by_alias=True)
    assert body == RESPONSES["C10GetDecisionEvidenceResponse"]["value"]


def test_recorded_alternative_is_returned(service, conn):
    record = _record()
    _save(service, conn, record)

    result = service.get_decision_evidence(conn, OPERATOR, _by_id(record), "opt-2")

    assert isinstance(result, Ok)
    assert result.value.kind == "Alternative"
    assert result.value.alternative == record.considered_alternatives[0]


def test_reference_outside_the_record_is_not_found(service, conn):
    record = _record()
    _save(service, conn, record)

    result = service.get_decision_evidence(conn, OPERATOR, _by_id(record), "opt-99")

    _assert_failure(result, "NotFound", "C10.EVIDENCE_NOT_FOUND")


# ---- storage failures ----


def test_unavailable_store_reports_unavailable():
    store = InMemoryDecisionStore()
    service = DecisionRecords(store, clock=lambda: RECORDED_AT)
    tx = store.begin()
    store.available = False

    for result in _read_all(service, tx, OPERATOR, _record()):
        _assert_failure(result, "Unavailable", "C10.UNAVAILABLE")


@pytest.mark.parametrize("opened", ["postgres"], indirect=True)
def test_closed_connection_reports_unavailable(service, conn):
    conn.close()

    for result in _read_all(service, conn, OPERATOR, _record()):
        _assert_failure(result, "Unavailable", "C10.UNAVAILABLE")


@pytest.mark.parametrize("opened", ["postgres"], indirect=True)
def test_stored_record_that_no_longer_validates_is_unavailable(service, conn):
    record = _record()
    _save(service, conn, record)
    # rows cannot be updated, so copy one with content that breaks the contract
    broken_id = uuid4()
    content = record.model_dump(mode="json", by_alias=True) | {"reasons": []}
    conn.execute(
        "insert into c10_decision_records.decisions"
        " (decision_id, idempotency_key, request_hash, producer, decision_type,"
        "  subject_type, subject_id, attempt_outcome, run_mode, decided_at,"
        "  recorded_at, record)"
        " select %s, 'broken', 'x', producer, decision_type, subject_type,"
        "  subject_id, attempt_outcome, run_mode, decided_at, recorded_at, %s"
        " from c10_decision_records.decisions",
        (broken_id, Jsonb(content)),
    )

    result = service.get_decision(
        conn, OPERATOR, DecisionIdSelector(decision_id=broken_id)
    )

    _assert_failure(result, "Unavailable", "C10.UNAVAILABLE")


def _store_unreadable(conn, record: DecisionRecord) -> None:
    """Add ``record`` with content that breaks the contract (rows cannot be
    updated, so it is inserted directly)."""
    content = record.model_dump(mode="json", by_alias=True) | {"reasons": []}
    conn.execute(
        "insert into c10_decision_records.decisions"
        " (decision_id, idempotency_key, request_hash, producer, decision_type,"
        "  subject_type, subject_id, subject_version, attempt_outcome, run_mode,"
        "  run_id, decided_at, recorded_at, record)"
        " values (%s, %s, 'x', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            record.decision_id,
            record.idempotency_key,
            record.producer,
            record.decision_type.value,
            record.subject.type.value,
            record.subject.id,
            record.subject.version,
            record.attempt_outcome.value,
            record.run_scope.mode.value,
            record.run_scope.run_id,
            record.decided_at,
            RECORDED_AT,
            Jsonb(content),
        ),
    )


@pytest.mark.parametrize("opened", ["postgres"], indirect=True)
@pytest.mark.parametrize(
    "elsewhere",
    [
        {"version": 1, "runScope": {"mode": "Simulation", "runId": str(OTHER_RUN)}},
        {"version": 1, "runScope": SIMULATED},
        {"version": 2, "runScope": LIVE},
    ],
    ids=["another run and version", "another version", "live"],
)
def test_unreadable_record_outside_the_selection_is_not_read(service, conn, elsewhere):
    wanted = _another(version=2, runScope=SIMULATED)
    _save(service, conn, wanted)
    _store_unreadable(conn, _another(**elsewhere))

    result = service.find_decision(
        conn, OPERATOR, _by_subject(wanted, subject_version=2, run_id=RUN)
    )

    assert isinstance(result, Ok)
    assert result.value.decision_id == wanted.decision_id


@pytest.mark.parametrize("opened", ["postgres"], indirect=True)
def test_unreadable_record_inside_the_selection_is_unavailable(service, conn):
    wanted = _another(version=2, runScope=SIMULATED)
    _save(service, conn, wanted)
    _store_unreadable(conn, _another(version=2, runScope=SIMULATED))

    result = service.find_decision(
        conn, OPERATOR, _by_subject(wanted, subject_version=2, run_id=RUN)
    )

    _assert_failure(result, "Unavailable", "C10.UNAVAILABLE")
