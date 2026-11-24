"""Walking skeleton hop 4, C10 side (#72, #60): C05 generatePlan passes the
EvidenceSnapshot from C09 queryKnowledge unchanged to C10 recordDecision,
in-process, on C05's own PostgreSQL transaction.

STUB: C05 is not on ``develop`` yet, so ``C05PlanStub`` stands in for it: it
saves a plan row and records the decision in one transaction, as
generatePlan will. The snapshot is the C09 contract example; the real
``query_knowledge`` call replaces it once C09 is wired in (#85).
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from app.components.c09_logistics_knowledge.contracts import EvidenceSnapshot
from app.components.c10_decision_records.contracts import DecisionRecord
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import DecisionRecords
from app.shared.c09_c10_common import AuthContext
from app.shared.logging import JsonFormatter
from app.shared.result import Ok, Result

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "components"
    / "c10-decision-records"
    / "examples"
    / "record-decision-request.json"
)


def _c09_snapshot() -> EvidenceSnapshot:
    """What C09 queryKnowledge returns for the hello-world query (HGV delivery
    in dublin-D2). STUB until the real call is wired in (#85)."""
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return EvidenceSnapshot.model_validate(data["evidenceSnapshots"][0])


class C05PlanStub:
    """STUB for C05 generatePlan: fixed plan, real transaction and C10 call."""

    def __init__(self, decisions: DecisionRecords) -> None:
        self._decisions = decisions

    def generate_plan(
        self, conn: psycopg.Connection, ctx: AuthContext, evidence: EvidenceSnapshot
    ) -> tuple[UUID, Result]:
        plan_id = uuid4()
        conn.execute("insert into c05_plans_stub (plan_id) values (%s)", (plan_id,))
        record = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        record.update(
            decisionId=str(uuid4()),
            idempotencyKey=f"c05:plan:{plan_id}:v1",
            subject={"type": "Plan", "id": str(plan_id), "version": 1},
            evidenceSnapshots=[evidence.model_dump(mode="json", by_alias=True)],
            decidedAt=datetime.now(UTC).isoformat(),
        )
        decision = DecisionRecord.model_validate(record)
        return plan_id, self._decisions.record_decision(conn, ctx, decision)


@pytest.fixture
def db(test_database_url: str):
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute(
            "truncate c10_decision_records.decisions,"
            " c10_decision_records.audit_outcomes"
        )
    return test_database_url


@pytest.fixture
def c05_conn(db: str):
    """C05's connection, with a session-local table standing in for its plans."""
    conn = psycopg.connect(db)
    conn.execute("create temp table c05_plans_stub (plan_id uuid primary key)")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def c05() -> C05PlanStub:
    return C05PlanStub(
        DecisionRecords(PostgresDecisionStore(), clock=lambda: datetime.now(UTC))
    )


@pytest.fixture
def ctx() -> AuthContext:
    return AuthContext(
        principal_id="service:c05-planner",
        principal_type="Service",
        roles=["Service:C05"],
        correlation_id=uuid4(),
    )


def _stored(db: str, plan_id: UUID) -> list[dict]:
    with psycopg.connect(db) as conn:
        rows = conn.execute(
            "select record from c10_decision_records.decisions where subject_id = %s",
            (plan_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _plan_saved(conn: psycopg.Connection, plan_id: UUID) -> bool:
    row = conn.execute(
        "select 1 from c05_plans_stub where plan_id = %s", (plan_id,)
    ).fetchone()
    return row is not None


def test_hop4_plan_and_decision_commit_together_with_evidence_unchanged(
    db, c05_conn, c05, ctx
):
    evidence = _c09_snapshot()

    plan_id, result = c05.generate_plan(c05_conn, ctx, evidence)
    c05_conn.commit()

    assert isinstance(result, Ok)
    assert result.value.duplicate is False
    assert _plan_saved(c05_conn, plan_id)
    [record] = _stored(db, plan_id)
    assert record["decisionId"] == str(result.value.decision_id)
    assert record["evidenceSnapshots"] == [
        evidence.model_dump(mode="json", by_alias=True)
    ]


def test_hop4_rollback_leaves_neither_plan_nor_decision(db, c05_conn, c05, ctx):
    plan_id, result = c05.generate_plan(c05_conn, ctx, _c09_snapshot())
    c05_conn.rollback()

    assert isinstance(result, Ok)
    assert not _plan_saved(c05_conn, plan_id)
    assert _stored(db, plan_id) == []


def test_hop4_c10_logs_the_callers_correlation_id(db, c05_conn, c05, ctx, caplog):
    caplog.set_level(logging.INFO, logger="app.components.c10_decision_records")

    c05.generate_plan(c05_conn, ctx, _c09_snapshot())
    c05_conn.commit()

    [record] = [r for r in caplog.records if r.name.startswith("app.components.c10_")]
    line = json.loads(JsonFormatter("C05").format(record))
    assert line["component"] == "C10"
    assert line["correlationId"] == str(ctx.correlation_id)
    assert line["scope"] == "live"
    assert "runId" not in line
