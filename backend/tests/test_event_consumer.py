"""Shared event consumer: decide ack / retry / reject per message (#73)."""

import json
import os
from uuid import uuid4

import pika
import pytest

from app.components.c06_city_monitoring.publisher import RabbitMQPublisher
from app.shared.contracts import EventEnvelope
from app.shared.events import EventConsumer, handle_delivery

RABBITMQ_URL = os.environ.get("TEST_RABBITMQ_URL")
needs_broker = pytest.mark.skipif(
    not RABBITMQ_URL, reason="TEST_RABBITMQ_URL not set; skipping RabbitMQ tests"
)


@pytest.fixture
def envelope(example) -> EventEnvelope:
    return EventEnvelope.model_validate(example("city-state-updated-event.json"))


def body_of(envelope: EventEnvelope) -> bytes:
    return envelope.model_dump_json(by_alias=True).encode()


class TestHandleDelivery:
    def test_valid_envelope_goes_to_the_handler(self, envelope):
        received = []

        disposition = handle_delivery(
            body_of(envelope), lambda e: received.append(e) or "ack"
        )

        assert disposition == "ack"
        assert received == [envelope]

    def test_handler_decides_to_retry(self, envelope):
        assert handle_delivery(body_of(envelope), lambda e: "retry") == "retry"

    @pytest.mark.parametrize(
        "body",
        [b"not json", b"{}", json.dumps({"eventId": "x"}).encode()],
        ids=["not-json", "empty-object", "missing-fields"],
    )
    def test_unreadable_messages_are_rejected_without_calling_the_handler(self, body):
        called = []

        disposition = handle_delivery(body, lambda e: called.append(e) or "ack")

        assert disposition == "reject"
        assert called == []

    def test_handler_crash_is_retried(self, envelope):
        def crash(_: EventEnvelope):
            raise RuntimeError("bug")

        assert handle_delivery(body_of(envelope), crash) == "retry"


@needs_broker
class TestEventConsumerWithRabbitMQ:
    @pytest.fixture
    def names(self):
        exchange, queue = f"ulop.events.test-{uuid4()}", f"test.queue-{uuid4()}"
        yield exchange, queue
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        channel = connection.channel()
        channel.queue_delete(queue)
        channel.exchange_delete(exchange)
        connection.close()

    def consumer(self, names, handler) -> EventConsumer:
        exchange, queue = names
        return EventConsumer(
            RABBITMQ_URL,
            queue=queue,
            event_types=["CityStateUpdated"],
            handler=handler,
            exchange=exchange,
        )

    def queued(self, names) -> int:
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        count = connection.channel().queue_declare(names[1], passive=True)
        connection.close()
        return count.method.message_count

    def test_published_event_is_consumed_and_acked(self, names, envelope):
        received = []
        consumer = self.consumer(names, lambda e: received.append(e) or "ack")
        consumer.declare()
        RabbitMQPublisher(RABBITMQ_URL, names[0]).publish(envelope)

        handled = consumer.consume_available()
        consumer.close()

        assert handled == 1
        assert received == [envelope]
        assert self.queued(names) == 0

    def test_retried_event_stays_queued(self, names, envelope):
        consumer = self.consumer(names, lambda e: "retry")
        consumer.declare()
        RabbitMQPublisher(RABBITMQ_URL, names[0]).publish(envelope)

        consumer.consume_available(max_messages=1)
        consumer.close()

        assert self.queued(names) == 1

    def test_unreadable_message_is_dropped(self, names):
        consumer = self.consumer(names, lambda e: "ack")
        consumer.declare()
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        connection.channel().basic_publish(
            exchange=names[0], routing_key="CityStateUpdated", body=b"garbage"
        )
        connection.close()

        consumer.consume_available()
        consumer.close()

        assert self.queued(names) == 0
