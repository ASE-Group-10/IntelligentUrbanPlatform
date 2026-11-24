"""Hop 5 using the HTTP app, committed C04 input, PostgreSQL and the C08 envelope."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from uuid import UUID, uuid4

import pika
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.components.c04_delivery_management.planning import seed_demo_delivery
from app.components.c05_planning.models import Plan, PlanScope, SavedDelivery
from app.components.c05_planning.publisher import PostgresPlanOutbox
from app.components.c08_allocation.events import PlanReadyEvent
from app.components.c09_logistics_knowledge.demo_corpus import load
from app.config import Settings
from app.main import create_app
from app.shared.publisher import RabbitMQPublisher, drain_outbox
from app.shared.result import Ok


@dataclass
class Flow:
    client: TestClient
    plan: Plan
    trace: UUID
    headers: dict[str, str]

    def confirm(self, **changes):
        body = {
            "expectedVersion": self.plan.version,
            "runScope": self.plan.run_scope.model_dump(mode="json", by_alias=True),
        }
        return self.client.post(
            f"/api/v1/plans/{self.plan.plan_id}/confirmation",
            headers=self.headers,
            json={**body, **changes},
        )


@pytest.fixture
def flow(example, test_database_url):
    run_id, trace, operator, provider = (uuid4() for _ in range(4))
    saved = example("delivery-submitted.json")
    saved["deliveryId"] = str(uuid4())
    with psycopg.connect(test_database_url) as conn:
        seed_demo_delivery(conn, run_id, saved)
        conn.execute(
            "insert into c02_identity_access.profiles "
            "values (%s, 'test operator', false)",
            (operator,),
        )
        conn.execute(
            "insert into c02_identity_access.providers values (%s, 'test provider')",
            (provider,),
        )
        conn.execute(
            "insert into c02_identity_access.provider_memberships "
            "values (%s, %s, 'operator')",
            (operator, provider),
        )

    class TestVerifier:
        def subject(self, token):
            assert token == "operator-session"
            return operator

    config = Settings(
        database_url="postgresql+psycopg://" + test_database_url.split("://", 1)[1],
        SHARED_RUN_IDS={run_id},
    )
    with TestClient(
        create_app(settings=config, identity_verifier=TestVerifier())
    ) as client:
        assert all(
            isinstance(result, Ok) for result in load(client.app.state.knowledge)
        )
        result = client.app.state.planning.generate_plan(
            SavedDelivery.model_validate(saved),
            PlanScope(scope="simulation", run_id=run_id),
            trace,
            "from-c04",
        )
        assert isinstance(result, Ok), result
        headers = {
            "Authorization": "Bearer operator-session",
            "Idempotency-Key": "operator-click",
            "X-Correlation-ID": str(trace),
            "X-Acting-Provider": str(provider),
        }
        yield Flow(client, result.value, trace, headers)


def state(conn, plan):
    delivery = conn.execute(
        "select delivery->>'status', (delivery->>'version')::int, plan_id "
        "from c04_delivery_management.deliveries where run_id=%s and delivery_id=%s",
        (plan.run_scope.run_id, plan.request_ids[0]),
    ).fetchone()
    versions = conn.execute(
        "select version, proposal->>'status' from c05_planning.plan_versions "
        "where plan_id=%s order by version",
        (plan.plan_id,),
    ).fetchall()
    events = conn.execute(
        "select event_id, envelope, published_at from c05_planning.outbox "
        "where plan_id=%s",
        (plan.plan_id,),
    ).fetchall()
    return delivery, versions, events


def test_concurrent_confirmations_link_once_and_replay_the_committed_response(
    flow, test_database_url
):
    gate = Barrier(2)

    def click():
        gate.wait(timeout=5)
        return flow.confirm()

    with ThreadPoolExecutor(2) as pool:
        responses = [pool.submit(click) for _ in range(2)]
        first, retry = (f.result(timeout=15) for f in responses)
    assert first.status_code == retry.status_code == 200, first.text
    assert first.json() == retry.json()
    assert first.json()["decisionId"] == str(flow.plan.decision_id)
    assert first.json()["version"] == 2 and first.json()["status"] == "confirmed"
    assert flow.confirm(expectedVersion=2).status_code == 409
    flow.headers["Idempotency-Key"] = "another-click"
    assert flow.confirm().status_code == 409
    with psycopg.connect(test_database_url) as conn:
        delivery, versions, events = state(conn, flow.plan)
        audit = conn.execute(
            "select count(*) from c10_decision_records.audit_outcomes where "
            "operation='C04.markRequestsPlanned' and correlation_id=%s",
            (flow.trace,),
        ).fetchone()
    assert delivery == ("planned", 2, flow.plan.plan_id)
    assert versions == [(1, "awaiting_confirmation"), (2, "confirmed")]
    assert len(events) == 1 and events[0][2] is None
    assert audit == (1,)


def test_outbox_insert_failure_rolls_back_link_version_receipt_and_success_audit(
    flow, test_database_url
):
    # On the dedicated test database, reject new inserts while preserving prior rows.
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        observer.execute(
            "alter table c05_planning.outbox add constraint test_refuse_event "
            "check (false) not valid"
        )
        try:
            response = flow.confirm()
            assert response.status_code == 503, response.text
            assert state(observer, flow.plan) == (
                ("pending", 1, None),
                [(1, "awaiting_confirmation")],
                [],
            )
            assert observer.execute(
                "select count(*) from c05_planning.confirmation_receipts "
                "where plan_id=%s",
                (flow.plan.plan_id,),
            ).fetchone() == (0,)
            assert observer.execute(
                "select count(*) from c10_decision_records.audit_outcomes where "
                "operation='C04.markRequestsPlanned' and correlation_id=%s "
                "and outcome='Ok'",
                (flow.trace,),
            ).fetchone() == (0,)
        finally:
            observer.execute(
                "alter table c05_planning.outbox drop constraint test_refuse_event"
            )
        assert flow.confirm().status_code == 200


def test_changed_delivery_or_unauthorised_run_cannot_confirm(flow, test_database_url):
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        denied = flow.confirm(runScope={"scope": "simulation", "runId": str(uuid4())})
        assert denied.status_code == 403
        conn.execute(
            "update c04_delivery_management.deliveries set delivery = "
            "jsonb_set(delivery, '{version}', '2') where delivery_id=%s",
            (flow.plan.request_ids[0],),
        )
        conflict = flow.confirm()
        assert conflict.status_code == 409, conflict.text
        assert state(conn, flow.plan) == (
            ("pending", 2, None),
            [(1, "awaiting_confirmation")],
            [],
        )
        assert conn.execute(
            "select outcome from c10_decision_records.audit_outcomes "
            "where operation='C04.markRequestsPlanned' and correlation_id=%s",
            (flow.trace,),
        ).fetchall() == [("Conflict",)]


@pytest.mark.skipif(
    not os.getenv("TEST_RABBITMQ_URL"), reason="requires a test RabbitMQ broker"
)
def test_operator_confirmation_reaches_rabbitmq_with_the_saved_event_id(
    flow, test_database_url
):
    url = os.environ["TEST_RABBITMQ_URL"]
    exchange = f"test.confirm.{uuid4()}"
    publisher = RabbitMQPublisher(url, exchange)
    with pika.BlockingConnection(pika.URLParameters(url)) as broker:
        ch = broker.channel()
        ch.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
        queue = ch.queue_declare(queue="", exclusive=True).method.queue
        ch.queue_bind(queue=queue, exchange=exchange, routing_key="PlanReady")
        try:
            # Isolate this publisher pass from previous tests on the test database.
            with psycopg.connect(test_database_url) as conn:
                conn.execute("truncate c05_planning.outbox")
            store = PostgresPlanOutbox(test_database_url)
            assert flow.confirm().status_code == 200
            assert flow.confirm().status_code == 200
            assert ch.basic_get(queue, auto_ack=True)[0] is None
            drained = drain_outbox(store, publisher)
            assert isinstance(drained, Ok) and drained.value.published == 1
            method, props, raw = ch.basic_get(queue, auto_ack=False)
            assert method is not None and method.routing_key == "PlanReady"
            event = PlanReadyEvent.model_validate_json(raw)
            with psycopg.connect(test_database_url) as conn:
                _, _, [(event_id, saved, published_at)] = state(conn, flow.plan)
            assert json.loads(raw) == saved
            assert PlanReadyEvent.model_validate_json(json.dumps(saved)) == event
            assert published_at is not None and props.message_id == str(
                event_id
            ) == str(event.event_id)
            assert (event.run_id, event.correlation_id) == (
                flow.plan.run_scope.run_id,
                flow.trace,
            )
            assert (event.payload.plan_id, event.payload.plan_version) == (
                flow.plan.plan_id,
                2,
            )
            assert event.payload.decision_id == flow.plan.decision_id
            assert event.payload.requirements == flow.plan.resource_needs
            ch.basic_ack(method.delivery_tag)
            assert ch.basic_get(queue, auto_ack=True)[0] is None
        finally:
            publisher.close()
            ch.exchange_delete(exchange=exchange)
