"""Deliver a committed C05 outbox row over AMQP, including a lost publication mark."""

import json
import os
from uuid import uuid4

import pika
import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.components.c05_planning.publisher import PostgresPlanOutbox
from app.components.c08_allocation.events import PlanReadyEvent
from app.shared.outbox import PostgresOutboxBatch
from app.shared.publisher import RabbitMQPublisher, drain_outbox
from app.shared.result import Failure


def test_committed_plan_ready_retries_with_the_same_envelope(
    test_database_url, example, monkeypatch
):
    rabbit = os.environ.get("TEST_RABBITMQ_URL")
    if not rabbit:
        pytest.skip("TEST_RABBITMQ_URL not set")

    exchange = f"ulop.test.c05.{uuid4()}"
    queue = f"{exchange}.plan-ready"
    store = PostgresPlanOutbox(test_database_url)
    publisher = RabbitMQPublisher(rabbit, exchange, queues={queue: ["PlanReady"]})
    plan = example("confirmed-plan.json")
    plan_id = plan["planId"] = str(uuid4())
    envelope = example("plan-ready-event.json")
    envelope.update(
        eventId=str(uuid4()),
        runId=plan["runScope"]["runId"],
        scope=plan["runScope"]["scope"],
        affected={
            "planIds": [plan_id],
            "resourceIds": [],
            "segmentKeys": plan["routes"][0]["segmentKeys"],
        },
        entityVersions={plan_id: plan["version"]},
    )
    envelope["payload"].update(
        planId=plan_id,
        decisionId=plan["decisionId"],
        requirements=plan["resourceNeeds"],
    )
    expected = PlanReadyEvent.model_validate_json(json.dumps(envelope))
    saved = expected.model_dump(mode="json", by_alias=True, exclude_none=True)

    with psycopg.connect(test_database_url, autocommit=True) as observer:
        observer.execute("truncate c05_planning.replan_receipts, c05_planning.outbox")
        with psycopg.connect(test_database_url) as writer:
            writer.execute(
                "insert into c05_planning.plan_versions "
                "(proposal, correlation_id, planned_departure_at) "
                "values (%s, %s, %s)",
                (Jsonb(plan), expected.correlation_id, expected.observed_at),
            )
            writer.execute(
                "insert into c05_planning.outbox "
                "(event_id, scope_key, plan_id, version, event_type, envelope) "
                "values (%s, %s, %s, %s, 'PlanReady', %s)",
                (
                    expected.event_id,
                    f"simulation:{expected.run_id}",
                    plan_id,
                    plan["version"],
                    Jsonb(saved),
                ),
            )
            assert drain_outbox(store, publisher).value.published == 0

        def published_at():
            return observer.execute(
                "select published_at from c05_planning.outbox where event_id = %s",
                (expected.event_id,),
            ).fetchone()[0]

        # The broker confirms, but PostgreSQL loses the mark. The persisted row
        # must remain pending for another pass, with the same event identity.
        def lose_mark(batch, event_id):
            raise psycopg.OperationalError(
                "lost the connection after broker confirmation"
            )

        try:
            with monkeypatch.context() as patch:
                patch.setattr(PostgresOutboxBatch, "mark_published", lose_mark)
                assert isinstance(drain_outbox(store, publisher), Failure)
            assert published_at() is None
            assert drain_outbox(store, publisher).value.published == 1
            assert published_at() is not None
            assert drain_outbox(store, publisher).value.published == 0

            # Start the consumer only now. The publisher's durable topology
            # retained both deliveries across the retry.
            with pika.BlockingConnection(pika.URLParameters(rabbit)) as connection:
                channel = connection.channel()
                for _ in range(2):
                    method, props, body = channel.basic_get(queue, auto_ack=False)
                    assert method is not None and method.routing_key == "PlanReady"
                    assert props.message_id == str(expected.event_id)
                    assert props.correlation_id == str(expected.correlation_id)
                    assert props.delivery_mode == pika.DeliveryMode.Persistent.value
                    assert PlanReadyEvent.model_validate_json(body) == expected
                    assert json.loads(body) == saved
                    channel.basic_ack(method.delivery_tag)
                assert channel.basic_get(queue)[0] is None
        finally:
            publisher.close()
            with pika.BlockingConnection(pika.URLParameters(rabbit)) as cleanup:
                channel = cleanup.channel()
                channel.queue_delete(queue)
                channel.exchange_delete(exchange)
