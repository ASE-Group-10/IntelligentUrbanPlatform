"""Shared RabbitMQ consumer for every component that receives events (#72, #73).

A consumer owns one durable queue bound to ``ulop.events`` for the event types
it handles (routing key = ``eventType``). For each message it validates the
``EventEnvelope`` and calls the component's handler, which answers with:

- ``ack``: done (including "already processed"); the message is removed.
- ``retry``: temporary failure; the message goes back to the queue.
- ``reject``: the message can never be handled; it is dropped.

Unreadable messages are rejected without calling the handler, and a handler
crash counts as ``retry``. Delivery is at least once, so duplicates are
normal: each component deduplicates on ``eventId`` in its own store, in the
same transaction as its work (C07 already does).

Use::

    consumer = EventConsumer(url, queue="c08.plan-ready",
                             event_types=["PlanReady"], handler=handle)
    consumer.run(stop_event)
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Literal

import pika
from pika.exceptions import AMQPError
from pydantic import ValidationError

from app.shared.contracts import EventEnvelope
from app.shared.logging import trace
from app.shared.topology import declare_queues

Disposition = Literal["ack", "retry", "reject"]
EventHandler = Callable[[EventEnvelope], Disposition]

DEFAULT_EXCHANGE = "ulop.events"

log = logging.getLogger(__name__)


def handle_delivery(body: bytes, handler: EventHandler) -> Disposition:
    """Decide what to do with one message body."""
    try:
        envelope = EventEnvelope.model_validate_json(body)
    except ValidationError as error:
        # No input: the first error can hold the whole message body.
        log.warning(
            "rejected unreadable message: %s",
            error.errors(include_url=False, include_input=False)[:1],
        )
        return "reject"
    try:
        disposition = handler(envelope)
    except Exception:
        log.exception("handler failed; will retry", extra=trace(envelope))
        return "retry"
    log.info("handled %s: %s", envelope.event_type, disposition, extra=trace(envelope))
    return disposition


class EventConsumer:
    """Consume ``event_types`` from ``exchange`` into ``queue`` (durable)."""

    def __init__(
        self,
        url: str,
        queue: str,
        event_types: list[str],
        handler: EventHandler,
        exchange: str = DEFAULT_EXCHANGE,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self._parameters = pika.URLParameters(url)
        self._queue = queue
        self._event_types = event_types
        self._handler = handler
        self._exchange = exchange
        self._retry_delay = retry_delay_seconds
        self._connection: pika.BlockingConnection | None = None
        self._channel = None

    def declare(self) -> None:
        """Create the exchange, the queue and its bindings if they do not exist."""
        declare_queues(
            self._open_channel(), self._exchange, {self._queue: self._event_types}
        )

    def consume_available(self, max_messages: int | None = None) -> int:
        """Handle messages already queued, then return how many were handled.

        Stops early after a ``retry`` so a failing message is not looped on.
        ``max_messages=0`` handles nothing.
        """
        if max_messages is not None and max_messages <= 0:
            return 0
        channel = self._open_channel()
        handled = 0
        try:
            for method, _, body in channel.consume(self._queue, inactivity_timeout=0.5):
                if method is None:
                    break
                disposition = self._settle(channel, method, body)
                handled += 1
                if disposition == "retry" or handled == max_messages:
                    break
        finally:
            channel.cancel()
        return handled

    def run(self, stop: threading.Event) -> None:
        """Consume until ``stop`` is set, reconnecting after broker failures."""
        while not stop.is_set():
            try:
                self.declare()
                channel = self._open_channel()
                for method, _, body in channel.consume(
                    self._queue, inactivity_timeout=1.0
                ):
                    if stop.is_set():
                        break
                    if method is None:
                        continue
                    if self._settle(channel, method, body) == "retry":
                        time.sleep(self._retry_delay)
                channel.cancel()
            except AMQPError as error:
                log.warning("broker unavailable, reconnecting: %r", error)
                self.close()
                stop.wait(self._retry_delay)

    def close(self) -> None:
        if self._connection is not None and self._connection.is_open:
            try:
                self._connection.close()
            except AMQPError:
                pass
        self._connection = None
        self._channel = None

    def _settle(self, channel, method, body: bytes) -> Disposition:
        disposition = handle_delivery(body, self._handler)
        if disposition == "ack":
            channel.basic_ack(method.delivery_tag)
        elif disposition == "retry":
            channel.basic_nack(method.delivery_tag, requeue=True)
        else:
            channel.basic_reject(method.delivery_tag, requeue=False)
        return disposition

    def _open_channel(self):
        if self._channel is None or not self._channel.is_open:
            self._connection = pika.BlockingConnection(self._parameters)
            self._channel = self._connection.channel()
            self._channel.basic_qos(prefetch_count=1)
        return self._channel
