"""Durable consumer queues on the ``ulop.events`` exchange.

Publishers declare these queues as well as the consumers, so each queue exists
before the first event goes out, whichever process starts first. A queue keeps
its events until its consumer reads them. Mandatory publishing alone is not
enough: it only checks that *some* queue matched, so a consumer that has not
started yet would miss events whenever another consumer's queue already exists.

Add a row here when a component starts consuming events.
"""

from collections.abc import Iterable, Mapping

SUBSCRIBER_QUEUES: Mapping[str, tuple[str, ...]] = {
    "c08.plan-ready": ("PlanReady",),
    "c07.city-state-updated": ("CityStateUpdated",),
}
"""Queue name → event types bound to it (routing key = event type)."""


def declare_queues(channel, exchange: str, queues: Mapping[str, Iterable[str]]) -> None:
    """Create the topic exchange, the durable queues and their bindings if missing."""
    channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
    for queue, event_types in queues.items():
        channel.queue_declare(queue=queue, durable=True)
        for event_type in event_types:
            channel.queue_bind(queue=queue, exchange=exchange, routing_key=event_type)
