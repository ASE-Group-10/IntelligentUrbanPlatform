"""RabbitMQPublisher against a real broker (skipped without TEST_RABBITMQ_URL)."""

import os
from uuid import uuid4

import pika
import pytest

from app.components.c06_city_monitoring.publisher import PublishError, RabbitMQPublisher
from app.shared.contracts import EventEnvelope

RABBITMQ_URL = os.environ.get("TEST_RABBITMQ_URL")
pytestmark = pytest.mark.skipif(
    not RABBITMQ_URL, reason="TEST_RABBITMQ_URL not set; skipping RabbitMQ tests"
)


@pytest.fixture
def exchange():
    """A throwaway exchange so tests never touch the shared ulop.events."""
    name = f"ulop.events.test-{uuid4()}"
    yield name
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    connection.channel().exchange_delete(name)
    connection.close()


def bound_queue(exchange: str, routing_key: str):
    connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    channel = connection.channel()
    channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
    queue = channel.queue_declare(queue="", exclusive=True).method.queue
    channel.queue_bind(queue=queue, exchange=exchange, routing_key=routing_key)
    return connection, channel, queue


def test_event_is_routed_by_event_type_with_its_id(exchange, example):
    envelope = EventEnvelope.model_validate(example("city-state-updated-event.json"))
    connection, channel, queue = bound_queue(exchange, "CityStateUpdated")
    publisher = RabbitMQPublisher(RABBITMQ_URL, exchange)

    publisher.publish(envelope)
    method, properties, body = channel.basic_get(queue, auto_ack=True)
    publisher.close()
    connection.close()

    assert method.routing_key == "CityStateUpdated"
    assert properties.message_id == str(envelope.event_id)
    assert properties.delivery_mode == pika.DeliveryMode.Persistent.value
    assert properties.headers["scope"] == "simulation"
    assert EventEnvelope.model_validate_json(body) == envelope


def test_other_event_types_do_not_reach_the_queue(exchange, example):
    envelope = EventEnvelope.model_validate(example("city-state-updated-event.json"))
    connection, channel, queue = bound_queue(exchange, "PlanReady")
    publisher = RabbitMQPublisher(RABBITMQ_URL, exchange)

    publisher.publish(envelope)
    method, _, _ = channel.basic_get(queue, auto_ack=True)
    publisher.close()
    connection.close()

    assert method is None


def test_unreachable_broker_raises_publish_error(example):
    envelope = EventEnvelope.model_validate(example("city-state-updated-event.json"))
    publisher = RabbitMQPublisher(
        "amqp://ulop:ulop@127.0.0.1:1/%2F?connection_attempts=1&socket_timeout=2"
    )

    with pytest.raises(PublishError):
        publisher.publish(envelope)
