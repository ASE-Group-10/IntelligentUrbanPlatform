"""Deliver transactional outbox envelopes to RabbitMQ with publisher confirms."""

import logging
import os
import signal
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

import pika
from pika.exceptions import AMQPError, UnroutableError

from app.shared.contracts import EventEnvelope
from app.shared.logging import configure_logging, trace
from app.shared.outbox import OutboxStore, OutboxUnavailableError
from app.shared.result import Failure, Ok, Result
from app.shared.topology import SUBSCRIBER_QUEUES, declare_queues

DEFAULT_EXCHANGE = "ulop.events"
DEFAULT_BATCH_SIZE = 100
DEFAULT_INTERVAL_SECONDS = 2.0

log = logging.getLogger(__name__)


class PublishError(Exception):
    """The event bus did not confirm the event; it stays in the outbox."""


class EventPublisher(Protocol):
    def publish(self, envelope: EventEnvelope) -> None:
        """Publish one event and return once the broker confirms it.

        Raises:
            PublishError: if the broker is unreachable, refuses the event, or no
                queue is bound for its event type.
        """
        ...


@dataclass(frozen=True)
class DrainReport:
    published: int
    stopped_by: str | None = None
    """Why the pass stopped early (for example a broker outage), if it did."""


def drain_outbox(
    store: OutboxStore,
    publisher: EventPublisher,
    limit: int = DEFAULT_BATCH_SIZE,
) -> Result[DrainReport]:
    """Publish up to ``limit`` pending events, oldest first.

    Stops at the first event the broker does not confirm; events already
    confirmed in this pass stay marked, the rest wait for the next pass.

    Returns:
        ``Ok(DrainReport)``, also when the broker was down (``stopped_by`` says
        why); ``Failure("Unavailable")`` if the store fails (marks of this pass
        are lost, so those events are sent again later).
    """
    published = 0
    stopped_by = None
    try:
        with store.outbox_batch(limit) as batch:
            for envelope in batch.events:
                try:
                    publisher.publish(envelope)
                except PublishError as error:
                    stopped_by = str(error)
                    break
                batch.mark_published(envelope.event_id)
                published += 1
                log.info("published %s", envelope.event_type, extra=trace(envelope))
    except OutboxUnavailableError as error:
        return Failure("Unavailable", str(error))
    return Ok(DrainReport(published=published, stopped_by=stopped_by))


class RabbitMQPublisher:
    """Publishes envelopes to a durable topic exchange with publisher confirms.

    Routing key is the ``eventType`` (contract ``events.yaml``). Publishes are
    mandatory: an event that reaches no queue raises ``PublishError`` and stays in
    the outbox. The connection is opened on first use and reopened after a failure.

    ``queues`` are the consumer queues to declare when the channel opens
    (``app.shared.topology``), so they hold events for consumers that have not
    started yet.
    """

    def __init__(
        self,
        url: str,
        exchange: str = DEFAULT_EXCHANGE,
        queues: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self._parameters = pika.URLParameters(url)
        self._exchange = exchange
        self._queues = queues or {}
        self._connection: pika.BlockingConnection | None = None
        self._channel = None

    def publish(self, envelope: EventEnvelope) -> None:
        try:
            channel = self._open_channel()
            channel.basic_publish(
                exchange=self._exchange,
                routing_key=envelope.event_type,
                # Preserve omitted optional fields in the saved envelope on a retry.
                body=envelope.model_dump_json(
                    by_alias=True, exclude_unset=True
                ).encode(),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=pika.DeliveryMode.Persistent,
                    message_id=str(envelope.event_id),
                    correlation_id=str(envelope.correlation_id),
                    type=envelope.event_type,
                    headers={
                        "scope": envelope.scope,
                        "runId": str(envelope.run_id) if envelope.run_id else None,
                    },
                ),
                mandatory=True,
            )
        except UnroutableError as error:
            raise PublishError(
                f"no queue is bound for {envelope.event_type} yet"
            ) from error
        except AMQPError as error:
            self.close()
            raise PublishError(f"broker unavailable: {error!r}") from error

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            try:
                self._connection.close()
            except AMQPError:
                pass
        self._connection = None
        self._channel = None

    def _open_channel(self):
        if self._channel is None or not self._channel.is_open:
            self._connection = pika.BlockingConnection(self._parameters)
            self._channel = self._connection.channel()
            self._channel.confirm_delivery()
            declare_queues(self._channel, self._exchange, self._queues)
        return self._channel


def run(
    store: OutboxStore,
    publisher: EventPublisher,
    stop: threading.Event,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Drain the outbox repeatedly until ``stop`` is set."""
    while not stop.is_set():
        result = drain_outbox(store, publisher)
        if isinstance(result, Failure):
            log.warning("outbox store unavailable: %s", result.message)
        elif result.value.published or result.value.stopped_by:
            log.info(
                "published %d event(s)%s",
                result.value.published,
                f"; stopped: {result.value.stopped_by}"
                if result.value.stopped_by
                else "",
            )
        if not (
            isinstance(result, Ok) and result.value.published == DEFAULT_BATCH_SIZE
        ):
            stop.wait(interval_seconds)


def serve(component: str, store: OutboxStore) -> None:  # pragma: no cover - worker
    configure_logging(component)
    publisher = RabbitMQPublisher(
        os.environ["RABBITMQ_URL"],
        os.environ.get("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
        queues=SUBSCRIBER_QUEUES,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("%s outbox publisher started", component)
    try:
        run(store, publisher, stop)
    finally:
        publisher.close()
