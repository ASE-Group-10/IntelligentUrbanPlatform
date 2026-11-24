"""Deliver a committed C05 outbox row over AMQP, including a lost publication mark."""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pika
import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.components.c05_planning.models import Plan
from app.components.c05_planning.publisher import PostgresPlanOutbox
from app.components.c05_planning.versions import save_ready_version
from app.components.c08_allocation.events import PlanReadyEvent
from app.shared.outbox import PostgresOutboxBatch
from app.shared.publisher import RabbitMQPublisher, drain_outbox
from app.shared.result import Failure


def test_later_version_waits_for_commit_of_earlier_publication(
    test_database_url, example
):
    store = PostgresPlanOutbox(test_database_url)
    plan = Plan.model_validate(example("confirmed-plan.json"), strict=False).model_copy(
        update={"plan_id": uuid4()}
    )
    now, trace = datetime.now(UTC), uuid4()
    with psycopg.connect(test_database_url) as writer:
        writer.execute("truncate c05_planning.replan_receipts, c05_planning.outbox")
        first = save_ready_version(writer, plan, now, trace, now)
    with store.outbox_batch(1) as initial:
        assert [e.event_id for e in initial.events] == [first]
        with psycopg.connect(test_database_url) as writer:
            revision = plan.model_copy(update={"version": plan.version + 1})
            second = save_ready_version(
                writer, revision, now, trace, now, causation_id=uuid4()
            )
            other_plan = plan.model_copy(update={"plan_id": uuid4()})
            other = save_ready_version(writer, other_plan, now, uuid4(), now)
            other_scope = plan.run_scope.model_copy(update={"run_id": uuid4()})
            other_run = plan.model_copy(
                update={"run_scope": other_scope, "version": revision.version}
            )
            separate = save_ready_version(writer, other_run, now, uuid4(), now)

        # An overlapping dispatcher can handle other plans and the same ID in
        # another run, but cannot send the revision ahead of the first event.
        with store.outbox_batch(10) as parallel:
            assert [e.event_id for e in parallel.events] == [other, separate]
            for event in parallel.events:
                parallel.mark_published(event.event_id)
        initial.mark_published(first)
        with store.outbox_batch(10) as before_commit:
            assert before_commit.events == []
    with store.outbox_batch(10) as after_commit:
        assert [e.event_id for e in after_commit.events] == [second]
        after_commit.mark_published(second)


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
