"""C07 worker: CityStateUpdated from the event bus drives evaluateChange (#73)."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pika
import pytest

from app.components.c06_city_monitoring.contracts import Observation
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c06_city_monitoring.publisher import (
    RabbitMQPublisher,
    drain_outbox,
)
from app.components.c06_city_monitoring.store import StateScope
from app.components.c07_disruption_detection.detection import DisruptionDetection
from app.components.c07_disruption_detection.planning_stub import (
    DEMO_PLAN_ID,
    DEMO_SEGMENT,
    DemoPlanningStub,
)
from app.components.c07_disruption_detection.store import (
    InMemoryDisruptionStore,
    InMemoryDisruptionTransaction,
)
from app.components.c07_disruption_detection.worker import (
    QUEUE,
    c07_handler,
    make_consumer,
)
from app.shared.contracts import EventEnvelope

NOW = datetime(2026, 11, 20, 8, 15, tzinfo=UTC)


@pytest.fixture
def planning() -> DemoPlanningStub:
    return DemoPlanningStub()


@pytest.fixture
def handler(planning):
    detection = DisruptionDetection(
        planning=planning, store=InMemoryDisruptionStore(), clock=lambda: NOW
    )
    return c07_handler(detection)


@pytest.fixture
def closure_event(example) -> EventEnvelope:
    return EventEnvelope.model_validate(example("city-state-updated-event.json"))


def test_closure_on_the_demo_segment_requests_one_replan(
    handler, planning, closure_event
):
    assert handler(closure_event) == "ack"

    assert len(planning.replan_calls) == 1
    request, correlation_id = planning.replan_calls[0]
    assert request.plan_id == DEMO_PLAN_ID
    assert correlation_id == closure_event.correlation_id


def test_redelivered_event_is_acked_without_a_second_replan(
    handler, planning, closure_event
):
    handler(closure_event)

    assert handler(closure_event) == "ack"
    assert len(planning.replan_calls) == 1


def test_store_failure_is_retried(planning, closure_event):
    from app.components.c07_disruption_detection.store import (
        DisruptionStoreUnavailableError,
    )

    class StoreDown(InMemoryDisruptionTransaction):
        def is_processed(self, event_id):
            raise DisruptionStoreUnavailableError("database unreachable")

    detection = DisruptionDetection(
        planning=planning,
        store=InMemoryDisruptionStore(transaction_type=StoreDown),
        clock=lambda: NOW,
    )

    assert c07_handler(detection)(closure_event) == "retry"
    assert planning.replan_calls == []


def test_payload_that_breaks_the_contract_is_rejected(handler, closure_event):
    broken = closure_event.model_copy(update={"payload": {"snapshotVersion": "x"}})

    assert handler(broken) == "reject"


@pytest.mark.skipif(
    not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_RABBITMQ_URL")),
    reason="needs TEST_DATABASE_URL and TEST_RABBITMQ_URL",
)
def test_closure_travels_from_c06_through_rabbitmq_to_c07(c06_database, planning):
    """Walking-skeleton hops 7 and 8 with real Postgres and RabbitMQ."""
    rabbit = os.environ["TEST_RABBITMQ_URL"]
    exchange = f"ulop.events.test-{uuid4()}"
    queue = f"{QUEUE}.test-{uuid4()}"
    detection = DisruptionDetection(
        planning=planning, store=InMemoryDisruptionStore(), clock=lambda: NOW
    )
    consumer = make_consumer(detection, rabbit, exchange=exchange, queue=queue)
    consumer.declare()
    c06_store = PostgresCityStateStore(c06_database)
    run = StateScope(scope="simulation", run_id=uuid4())
    correlation_id = uuid4()
    observation = Observation.model_validate(
        {
            "observationId": str(uuid4()),
            "sourceId": "sim-traffic",
            "kind": "closure",
            "target": {"segmentKey": DEMO_SEGMENT},
            "values": {"status": "closed", "cause": "roadworks"},
            "observedAt": "2026-11-20T08:14:30Z",
        }
    )

    try:
        CityMonitoring(store=c06_store, clock=lambda: NOW).ingest_observation(
            SourceContext(state_scope=run, correlation_id=correlation_id), observation
        )
        drain_outbox(c06_store, RabbitMQPublisher(rabbit, exchange))
        consumer.consume_available()
    finally:
        consumer.close()
        connection = pika.BlockingConnection(pika.URLParameters(rabbit))
        channel = connection.channel()
        channel.queue_delete(queue)
        channel.exchange_delete(exchange)
        connection.close()

    assert len(planning.replan_calls) == 1
    request, received_correlation = planning.replan_calls[0]
    assert received_correlation == correlation_id
    assert request.plan_id == DEMO_PLAN_ID
