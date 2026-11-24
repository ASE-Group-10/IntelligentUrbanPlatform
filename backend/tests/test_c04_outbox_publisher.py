"""Tests for C04 delivery outbox ordering and RabbitMQ publication (#124)."""

import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pika
import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.components.c04_delivery_management.contracts import DeliveryEvent
from app.components.c04_delivery_management.publisher import PostgresDeliveryOutbox
from app.shared.outbox import OutboxUnavailableError, PostgresOutboxBatch
from app.shared.publisher import RabbitMQPublisher, drain_outbox
from app.shared.result import Failure
from app.shared.topology import SUBSCRIBER_QUEUES


def _make_event(
    example,
    *,
    delivery_id: UUID,
    version: int,
    provider_id: UUID,
    event_type: str = "DeliverySubmitted",
    scope: str = "live",
    run_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> tuple[DeliveryEvent, dict]:
    raw = example("delivery-submitted-event.json")
    raw["eventId"] = str(uuid4())
    raw["eventType"] = event_type
    raw["correlationId"] = str(correlation_id or uuid4())
    raw["scope"] = scope
    raw["runId"] = str(run_id) if run_id is not None else None
    raw["entityVersions"] = {str(delivery_id): version}
    raw["payload"]["deliveryId"] = str(delivery_id)
    raw["payload"]["deliveryVersion"] = version
    raw["payload"]["providerId"] = str(provider_id)
    raw["payload"]["delivery"]["deliveryId"] = str(delivery_id)
    raw["payload"]["delivery"]["version"] = version
    parsed = DeliveryEvent.model_validate(raw)
    wire = parsed.model_dump(mode="json", by_alias=True)
    wire["payload"]["delivery"] = parsed.payload.delivery.to_wire()
    return parsed, wire


def _insert_outbox(conn: psycopg.Connection, event: DeliveryEvent, wire: dict) -> None:
    scope_key = "live" if event.scope == "live" else f"simulation:{event.run_id}"
    conn.execute(
        """
        insert into c04_delivery_management.outbox (
            event_id,
            scope_key,
            delivery_id,
            delivery_version,
            event_type,
            correlation_id,
            envelope,
            created_at
        ) values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event.event_id,
            scope_key,
            event.payload.delivery_id,
            event.payload.delivery_version,
            event.event_type,
            event.correlation_id,
            Jsonb(wire),
            datetime.now(UTC),
        ),
    )


def test_topology_declares_c11_and_c12_delivery_queues():
    expected = ("DeliverySubmitted", "DeliveryUpdated", "DeliveryProgressChanged")
    assert SUBSCRIBER_QUEUES["c11.delivery-observations"] == expected
    assert SUBSCRIBER_QUEUES["c12.delivery-recordings"] == expected


def test_postgres_outbox_wraps_connection_error(monkeypatch):
    def fail_connect(*args, **kwargs):
        raise psycopg.OperationalError("database unreachable")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    store = PostgresDeliveryOutbox("postgres://invalid/ulop_test")
    with pytest.raises(OutboxUnavailableError):
        with store.outbox_batch(10):
            pass


def test_later_delivery_version_waits_for_earlier_publication(c04_database, example):
    store = PostgresDeliveryOutbox(c04_database)
    provider_id = uuid4()
    delivery_a = uuid4()
    delivery_b = uuid4()
    run_id = uuid4()

    v1, v1_wire = _make_event(
        example, delivery_id=delivery_a, version=1, provider_id=provider_id
    )
    v2, v2_wire = _make_event(
        example,
        delivery_id=delivery_a,
        version=2,
        provider_id=provider_id,
        event_type="DeliveryUpdated",
    )
    other, other_wire = _make_event(
        example, delivery_id=delivery_b, version=1, provider_id=provider_id
    )
    other_scope, other_scope_wire = _make_event(
        example,
        delivery_id=delivery_a,
        version=2,
        provider_id=provider_id,
        event_type="DeliveryUpdated",
        scope="simulation",
        run_id=run_id,
    )

    with psycopg.connect(c04_database) as writer:
        _insert_outbox(writer, v1, v1_wire)

    with store.outbox_batch(1) as initial:
        assert [e.event_id for e in initial.events] == [v1.event_id]
        with psycopg.connect(c04_database) as writer:
            _insert_outbox(writer, v2, v2_wire)
            _insert_outbox(writer, other, other_wire)
            _insert_outbox(writer, other_scope, other_scope_wire)

        # A concurrent publisher can claim other deliveries and the same
        # deliveryId in a different run scope, but not v2 of delivery_a yet.
        with store.outbox_batch(10) as parallel:
            assert [e.event_id for e in parallel.events] == [
                other.event_id,
                other_scope.event_id,
            ]
            for event in parallel.events:
                parallel.mark_published(event.event_id)

        initial.mark_published(v1.event_id)
        with store.outbox_batch(10) as before_commit:
            assert before_commit.events == []

    with store.outbox_batch(10) as after_commit:
        assert [e.event_id for e in after_commit.events] == [v2.event_id]
        after_commit.mark_published(v2.event_id)


def test_committed_delivery_event_publishes_and_retries_with_same_envelope(
    c04_database, example, monkeypatch
):
    rabbit = os.environ.get("TEST_RABBITMQ_URL")
    if not rabbit:
        pytest.skip("TEST_RABBITMQ_URL not set")

    exchange = f"ulop.test.c04.{uuid4()}"
    queue = f"{exchange}.delivery-observations"
    store = PostgresDeliveryOutbox(c04_database)
    publisher = RabbitMQPublisher(
        rabbit,
        exchange,
        queues={
            queue: [
                "DeliverySubmitted",
                "DeliveryUpdated",
                "DeliveryProgressChanged",
            ]
        },
    )
    provider_id = uuid4()
    delivery_id = uuid4()
    correlation_id = uuid4()
    expected, saved = _make_event(
        example,
        delivery_id=delivery_id,
        version=1,
        provider_id=provider_id,
        correlation_id=correlation_id,
    )

    with psycopg.connect(c04_database, autocommit=True) as observer:
        with psycopg.connect(c04_database) as writer:
            _insert_outbox(writer, expected, saved)
            assert drain_outbox(store, publisher).value.published == 0

        def published_at():
            return observer.execute(
                "select published_at from c04_delivery_management.outbox "
                "where event_id = %s",
                (expected.event_id,),
            ).fetchone()[0]

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

            with pika.BlockingConnection(pika.URLParameters(rabbit)) as connection:
                channel = connection.channel()
                for _ in range(2):
                    method, props, body = channel.basic_get(queue, auto_ack=False)
                    assert method is not None
                    assert method.routing_key == "DeliverySubmitted"
                    assert props.message_id == str(expected.event_id)
                    assert props.correlation_id == str(correlation_id)
                    assert props.delivery_mode == pika.DeliveryMode.Persistent.value
                    parsed = DeliveryEvent.model_validate_json(body)
                    assert parsed == expected
                    assert parsed.payload.provider_id == provider_id
                    assert json.loads(body)["payload"]["providerId"] == str(provider_id)
                    channel.basic_ack(method.delivery_tag)
                assert channel.basic_get(queue)[0] is None
        finally:
            publisher.close()
            with pika.BlockingConnection(pika.URLParameters(rabbit)) as cleanup:
                channel = cleanup.channel()
                channel.queue_delete(queue)
                channel.exchange_delete(exchange)
