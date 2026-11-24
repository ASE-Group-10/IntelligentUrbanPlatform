"""Verify the evidence checker against committed Dublin generation and replanning."""

from datetime import timedelta
from functools import partial
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c07_disruption_detection.worker import make_detection
from app.components.c10_decision_records.contracts import DecisionRecord
from app.components.c10_decision_records.service import DecisionRecords
from app.demo.c05_evidence import collect_evidence
from app.demo.feed import feed_closure, load_scenario
from app.shared.contracts import EventEnvelope
from app.shared.result import Ok
from tests.c05_flow import flow as flow


@pytest.fixture
def conn(test_database_url):
    with psycopg.connect(test_database_url, autocommit=True) as connection:
        yield connection


def evidence_for(conn, flow):
    return partial(
        collect_evidence,
        conn,
        run_id=flow.plan.run_scope.run_id,
        correlation_id=flow.trace,
        plan_id=flow.plan.plan_id,
    )


def mark_plan_published(conn, flow, version):
    # This suite verifies persisted evidence. Actual RabbitMQ receipt is checked
    # independently by the checker and the existing C05 broker integration tests.
    return conn.execute(
        """update c05_planning.outbox set published_at=now()
            where plan_id=%s and version=%s returning event_id""",
        (flow.plan.plan_id, version),
    ).fetchone()[0]


def test_evidence_tracks_one_delivery_from_proposal_to_closure(
    flow, conn, c06_database, c07_database
):
    read = evidence_for(conn, flow)
    proposal = read()
    assert proposal["origin"] == "saved-delivery"
    assert proposal["generation"]["ok"], proposal
    assert proposal["generation"]["decisionId"] == str(flow.plan.decision_id)
    assert proposal["generation"]["deliveryId"] == str(flow.plan.request_ids[0])
    assert not proposal["confirmation"]["ok"]
    assert not proposal["revision"]["ok"]

    assert flow.confirm().status_code == 200
    assert not read()["confirmation"]["ok"]  # Outbox insert alone is insufficient.
    first = mark_plan_published(conn, flow, 2)
    confirmed = read()
    assert confirmed["confirmation"]["ok"], confirmed
    assert confirmed["confirmation"]["eventId"] == str(first)
    assert confirmed["confirmation"]["decisionId"] == str(flow.plan.decision_id)

    scenario = load_scenario()

    def clock():
        return scenario.closure.observed_at + timedelta(seconds=5)

    result = feed_closure(
        PostgresCityStateStore(c06_database),
        scenario,
        clock,
        run_id=flow.plan.run_scope.run_id,
        correlation_id=flow.trace,
    )
    assert isinstance(result, Ok), result
    closure_id = result.value.queued_event_id
    (raw,) = conn.execute(
        """update c06_city_monitoring.outbox set published_at=now()
            where event_id=%s returning envelope""",
        (closure_id,),
    ).fetchone()
    evaluated = make_detection(c07_database, clock=clock).evaluate_change(
        EventEnvelope.model_validate(raw)
    )
    assert isinstance(evaluated, Ok) and evaluated.value.kind == "created", evaluated
    assert not read(closure_event_id=closure_id)["revision"]["ok"]

    second = mark_plan_published(conn, flow, 3)
    full = read(closure_event_id=closure_id)
    assert full["revision"]["ok"], full
    assert [full[k]["version"] for k in ("generation", "confirmation", "revision")] == [
        1,
        2,
        3,
    ]
    assert first != second
    assert full["confirmation"]["eventId"] == str(first)
    assert full["revision"]["eventId"] == str(second)
    assert full["revision"]["decisionId"] != full["generation"]["decisionId"]
    # C05 has committed the revision, but C07 may still be recording which
    # plans received PlanSaved. Another plan's success cannot complete this one.
    other = {
        "planId": str(uuid4()),
        "planVersion": 2,
        "replanRequested": True,
        "replanOutcome": "PlanSaved",
    }
    target = {**other, "planId": str(flow.plan.plan_id)}
    with conn.transaction(force_rollback=True):
        for fields, pending in [
            ({"replanRequested": False, "replanOutcome": None}, True),
            ({"replanOutcome": "NoChange"}, False),
        ]:
            conn.execute(
                """update c07_disruption_detection.disruptions
                      set disruption=jsonb_set(disruption, '{affectedPlans}', %s)
                    where scope_key=%s and disruption_id=%s""",
                (
                    Jsonb([other, {**target, **fields}]),
                    f"simulation:{flow.plan.run_scope.run_id}",
                    evaluated.value.disruption_id,
                ),
            )
            revision = read(closure_event_id=closure_id)["revision"]
            assert not revision["ok"]
            assert bool(revision.get("pendingOutcome")) == pending
    assert not read(closure_event_id=uuid4())["revision"]["ok"]
    for key in ("run_id", "correlation_id", "plan_id"):
        other = read(closure_event_id=closure_id, **{key: uuid4()})
        assert not any(
            other[k]["ok"] for k in ("generation", "confirmation", "revision")
        )

    conn.execute(
        "delete from c05_planning.replan_receipts where event_id=%s", (second,)
    )
    assert not read(closure_event_id=closure_id)["revision"]["ok"]


@pytest.mark.parametrize("missing", ["c05_receipt", "c04_receipt", "link", "envelope"])
def test_published_confirmation_still_requires_its_link_receipt_and_payload(
    flow, conn, missing
):
    assert flow.confirm().status_code == 200
    mark_plan_published(conn, flow, 2)
    read = evidence_for(conn, flow)
    assert read()["confirmation"]["ok"]
    if missing == "c05_receipt":
        conn.execute(
            "delete from c05_planning.confirmation_receipts where plan_id=%s",
            (flow.plan.plan_id,),
        )
    elif missing == "c04_receipt":
        conn.execute(
            """delete from c04_delivery_management.planning_receipts
                where run_id=%s and receipt->>'planId'=%s""",
            (flow.plan.run_scope.run_id, str(flow.plan.plan_id)),
        )
    elif missing == "link":
        conn.execute(
            """update c04_delivery_management.deliveries
                  set plan_id=null where plan_id=%s""",
            (flow.plan.plan_id,),
        )
    else:
        conn.execute(
            """update c05_planning.outbox set envelope=jsonb_set(
                 envelope, '{payload,decisionId}', to_jsonb(%s::text))
                where plan_id=%s and version=2""",
            (str(uuid4()), flow.plan.plan_id),
        )
    assert not read()["confirmation"]["ok"]


def test_c09_query_requires_evidence_on_the_actual_saved_decision(
    request, conn, monkeypatch
):
    record = DecisionRecords.record_decision

    def omit_policy(self, connection, ctx, decision):
        body = decision.model_dump(mode="json", by_alias=True)
        body["inputVersions"]["corpusVersion"] = None
        body["evidenceSnapshots"] = []
        for reason in body["reasons"]:
            reason["evidenceRefIds"] = []
        return record(self, connection, ctx, DecisionRecord.model_validate(body))

    # The C09 query still runs inside generatePlan. Simulate losing its evidence
    # on the C05 → C10 handoff before C10's insert-only record is committed.
    monkeypatch.setattr(DecisionRecords, "record_decision", omit_policy)
    generated = request.getfixturevalue("flow")
    assert not evidence_for(conn, generated)()["generation"]["ok"]


def test_confirmed_delivery_cannot_substantiate_a_different_generation_input(
    flow, conn
):
    assert flow.confirm().status_code == 200
    read = evidence_for(conn, flow)
    assert read()["generation"]["ok"]
    conn.execute(
        """update c05_planning.generation_receipts set request_body=jsonb_set(
             request_body, '{cargo,weightKg}', '99')
            where plan_id=%s""",
        (flow.plan.plan_id,),
    )
    assert not read()["generation"]["ok"]
