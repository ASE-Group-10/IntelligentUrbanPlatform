"""UC10 recordDecision / recordOutcome acceptance examples (#60), in-memory store."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.components.c10_decision_records.contracts import (
    AttemptOutcome,
    AuditOutcome,
    DecisionRecord,
)
from app.components.c10_decision_records.service import (
    DecisionRecords,
    parse_decision_record,
)
from app.components.c10_decision_records.store import (
    InMemoryDecisionStore,
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


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _record(name: str = "record-decision-request.json", **changes) -> DecisionRecord:
    data = _example(name)
    data.update(changes)
    return DecisionRecord.model_validate(data)


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


@pytest.fixture
def store() -> InMemoryDecisionStore:
    return InMemoryDecisionStore()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(store, clock) -> DecisionRecords:
    return DecisionRecords(store, clock=clock, new_id=lambda: AUDIT_ID)


def _record_and_commit(service, store, ctx, record):
    tx = store.begin()
    result = service.record_decision(tx, ctx, record)
    tx.commit()
    return result


# ---- recordDecision: stored with the caller's transaction ----


def test_selected_decision_is_stored_when_the_caller_commits(service, store):
    record = _record()
    tx = store.begin()

    result = service.record_decision(tx, C05, record)

    assert isinstance(result, Ok)
    assert result.value.decision_id == record.decision_id
    assert result.value.recorded_at == NOW
    assert result.value.duplicate is False
    assert store.decisions == {}  # C10 never commits on its own
    tx.commit()
    stored = store.decisions[record.decision_id]
    assert stored.record == record  # evidence snapshots kept exactly as received


def test_nothing_remains_when_the_caller_rolls_back(service, store):
    tx = store.begin()
    assert isinstance(service.record_decision(tx, C05, _record()), Ok)

    tx.rollback()

    assert store.decisions == {}


def test_closed_transaction_cannot_be_reused(service, store):
    tx = store.begin()
    tx.commit()
    with pytest.raises(TransactionClosedError):
        service.record_decision(tx, C05, _record())


# ---- recordDecision: idempotency ----


def test_same_key_and_body_returns_the_original_decision(service, store, clock):
    record = _record()
    _record_and_commit(service, store, C05, record)
    clock.now = LATER

    again = _record_and_commit(service, store, C05, record)

    assert isinstance(again, Ok)
    assert again.value.duplicate is True
    assert again.value.decision_id == record.decision_id
    assert again.value.recorded_at == NOW  # the original time, not the retry time
    assert len(store.decisions) == 1


def test_duplicate_is_detected_inside_the_same_transaction(service, store):
    tx = store.begin()
    service.record_decision(tx, C05, _record())

    again = service.record_decision(tx, C05, _record())

    assert isinstance(again, Ok) and again.value.duplicate is True
    assert len(tx.decisions) == 1


def test_same_key_with_another_body_is_a_conflict(service, store):
    _record_and_commit(service, store, C05, _record())
    changed = _record(limitations=["recomputed"])

    result = _record_and_commit(service, store, C05, changed)

    assert isinstance(result, Failure)
    assert result.kind == "Conflict"
    assert result.message.startswith("C10.IDEMPOTENCY_MISMATCH")


def test_decision_id_reused_under_another_key_is_a_conflict(service, store):
    _record_and_commit(service, store, C05, _record())
    reused = _record(idempotencyKey="c05:plan:other:v1")

    result = _record_and_commit(service, store, C05, reused)

    assert isinstance(result, Failure) and result.kind == "Conflict"
    assert len(store.decisions) == 1


# ---- recordDecision: attempts that selected nothing (UC03 AF2) ----


def test_failed_attempt_is_stored_without_option_or_version(service, store):
    record = _record("record-decision-data-unavailable-request.json")

    result = _record_and_commit(service, store, C05, record)

    assert isinstance(result, Ok)
    stored = store.decisions[record.decision_id].record
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
def test_producer_must_match_the_calling_service(service, store, ctx):
    result = _record_and_commit(service, store, ctx, _record())

    assert isinstance(result, Failure)
    assert result.kind == "Forbidden"
    assert result.message.startswith("C10.PRODUCER_MISMATCH")
    assert store.decisions == {}


# ---- recordDecision: storage outage ----


def test_unavailable_store_reports_unavailable(service, store):
    tx = store.begin()
    store.available = False

    result = service.record_decision(tx, C05, _record())

    assert isinstance(result, Failure) and result.kind == "Unavailable"


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


# ---- recordOutcome ----


def _outcome() -> AuditOutcome:
    return AuditOutcome.model_validate(_example("record-outcome-request.json"))


def test_outcome_is_stored_with_principal_and_correlation_from_context(service, store):
    ctx = _ctx("User", "LogisticsOperator")
    tx = store.begin()

    result = service.record_outcome(tx, ctx, _outcome())
    tx.commit()

    assert isinstance(result, Ok) and result.value.audit_id == AUDIT_ID
    [entry] = store.outcomes
    assert entry.outcome == _outcome()
    assert entry.principal_id == ctx.principal_id
    assert entry.correlation_id == ctx.correlation_id
    assert entry.recorded_at == NOW


def test_outcome_rolls_back_with_the_caller(service, store):
    tx = store.begin()
    service.record_outcome(tx, C05, _outcome())

    tx.rollback()

    assert store.outcomes == []


def test_outcome_with_unavailable_store_reports_unavailable(service, store):
    tx = store.begin()
    store.available = False

    result = service.record_outcome(tx, C05, _outcome())

    assert isinstance(result, Failure) and result.kind == "Unavailable"
