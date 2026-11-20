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
from app.shared.result import Failure

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


class FlakyPlanning(DemoPlanningStub):
    """The demo stub, but C05 can be switched off."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def find_affected_work(self, scope):
        if self.down:
            return Failure("Unavailable", "C05 is down")
        return super().find_affected_work(scope)


def test_closure_is_retried_while_c05_is_down_and_handled_once_it_is_back(
    closure_event,
):
    planning = FlakyPlanning()
    detection = DisruptionDetection(
        planning=planning, store=InMemoryDisruptionStore(), clock=lambda: NOW
    )
    handle = c07_handler(detection)

    assert handle(closure_event) == "retry"
    planning.down = False
    assert handle(closure_event) == "ack"
    assert len(planning.replan_calls) == 1


class ReplanDown(DemoPlanningStub):
    """The demo stub, but C05 refuses replan requests while ``down``."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def request_replan(self, request, correlation_id):
        if self.down:
            return Failure("Unavailable", "C05 is down")
        return super().request_replan(request, correlation_id)


def test_replan_refused_by_c05_is_retried_until_c05_takes_it(closure_event):
    planning = ReplanDown()
    detection = DisruptionDetection(
        planning=planning, store=InMemoryDisruptionStore(), clock=lambda: NOW
    )
    handle = c07_handler(detection)

    assert handle(closure_event) == "retry"
    assert handle(closure_event) == "retry"
    planning.down = False
    assert handle(closure_event) == "ack"
    assert handle(closure_event) == "ack"
    assert len(planning.replan_calls) == 1


class PlanGone(DemoPlanningStub):
    """The demo stub, but C05 no longer knows the plan."""

    def request_replan(self, request, correlation_id):
        return Failure("NotFound", "plan does not exist")


def test_plan_that_c05_does_not_know_is_acked_and_not_retried(closure_event):
    detection = DisruptionDetection(
        planning=PlanGone(), store=InMemoryDisruptionStore(), clock=lambda: NOW
    )

    assert c07_handler(detection)(closure_event) == "ack"


def test_unknown_outcome_that_a_retry_cannot_fix_is_acked(handler, closure_event):
    other_type = closure_event.model_copy(update={"event_type": "PlanReady"})

    assert handler(other_type) == "ack"


def test_payload_that_breaks_the_contract_is_rejected(handler, closure_event):
    broken = closure_event.model_copy(update={"payload": {"snapshotVersion": "x"}})

    assert handler(broken) == "reject"


needs_db_and_broker = pytest.mark.skipif(
    not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_RABBITMQ_URL")),
    reason="needs TEST_DATABASE_URL and TEST_RABBITMQ_URL",
)


def closure_observation() -> Observation:
    return Observation.model_validate(
        {
            "observationId": str(uuid4()),
            "sourceId": "sim-traffic",
            "kind": "closure",
            "target": {"segmentKey": DEMO_SEGMENT},
            "values": {"status": "closed", "cause": "roadworks"},
            "observedAt": "2026-11-20T08:14:30Z",
        }
    )


def delete_broker_objects(rabbit: str, exchange: str, queue: str) -> None:
    connection = pika.BlockingConnection(pika.URLParameters(rabbit))
    channel = connection.channel()
    channel.queue_delete(queue)
    channel.exchange_delete(exchange)
    connection.close()


@needs_db_and_broker
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
    observation = closure_observation()

    try:
        CityMonitoring(store=c06_store, clock=lambda: NOW).ingest_observation(
            SourceContext(state_scope=run, correlation_id=correlation_id), observation
        )
        drain_outbox(c06_store, RabbitMQPublisher(rabbit, exchange))
        consumer.consume_available()
    finally:
        consumer.close()
        delete_broker_objects(rabbit, exchange, queue)

    assert len(planning.replan_calls) == 1
    request, received_correlation = planning.replan_calls[0]
    assert received_correlation == correlation_id
    assert request.plan_id == DEMO_PLAN_ID


@needs_db_and_broker
def test_closure_published_before_c07_starts_still_reaches_c07(c06_database, planning):
    """Fresh broker, C06 first: the closure waits in the outbox until C07 binds."""
    rabbit = os.environ["TEST_RABBITMQ_URL"]
    exchange = f"ulop.events.test-{uuid4()}"
    queue = f"{QUEUE}.test-{uuid4()}"
    c06_store = PostgresCityStateStore(c06_database)
    run = StateScope(scope="simulation", run_id=uuid4())
    publisher = RabbitMQPublisher(rabbit, exchange)
    detection = DisruptionDetection(
        planning=planning, store=InMemoryDisruptionStore(), clock=lambda: NOW
    )
    consumer = make_consumer(detection, rabbit, exchange=exchange, queue=queue)

    try:
        CityMonitoring(store=c06_store, clock=lambda: NOW).ingest_observation(
            SourceContext(state_scope=run, correlation_id=uuid4()),
            closure_observation(),
        )
        before_c07 = drain_outbox(c06_store, publisher).value
        consumer.declare()  # C07 starts and binds its queue
        after_c07 = drain_outbox(c06_store, publisher).value
        consumer.consume_available()
    finally:
        publisher.close()
        consumer.close()
        delete_broker_objects(rabbit, exchange, queue)

    assert before_c07.published == 0
    assert "no queue is bound" in before_c07.stopped_by
    assert after_c07.published == 1
    assert len(planning.replan_calls) == 1


@needs_db_and_broker
def test_closure_reaches_c07_started_late_while_another_consumer_listens(
    c06_database, planning
):
    """Another subscriber already bound makes the publish routable; C07's queue,
    declared by the publisher, still holds the closure until C07 starts."""
    rabbit = os.environ["TEST_RABBITMQ_URL"]
    exchange = f"ulop.events.test-{uuid4()}"
    queue = f"{QUEUE}.test-{uuid4()}"
    other_queue = f"c11.test-{uuid4()}"
    connection = pika.BlockingConnection(pika.URLParameters(rabbit))
    other = connection.channel()
    other.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
    other.queue_declare(queue=other_queue, exclusive=True)
    other.queue_bind(
        queue=other_queue, exchange=exchange, routing_key="CityStateUpdated"
    )
    c06_store = PostgresCityStateStore(c06_database)
    publisher = RabbitMQPublisher(
        rabbit, exchange, queues={queue: ["CityStateUpdated"]}
    )
    detection = DisruptionDetection(
        planning=planning, store=InMemoryDisruptionStore(), clock=lambda: NOW
    )
    consumer = make_consumer(detection, rabbit, exchange=exchange, queue=queue)

    try:
        CityMonitoring(store=c06_store, clock=lambda: NOW).ingest_observation(
            SourceContext(
                state_scope=StateScope(scope="simulation", run_id=uuid4()),
                correlation_id=uuid4(),
            ),
            closure_observation(),
        )
        drained = drain_outbox(c06_store, publisher).value
        consumer.declare()  # C07 starts only now
        consumer.consume_available()
    finally:
        publisher.close()
        consumer.close()
        other.queue_delete(other_queue)
        connection.close()
        delete_broker_objects(rabbit, exchange, queue)

    assert drained.published == 1
    assert len(planning.replan_calls) == 1
