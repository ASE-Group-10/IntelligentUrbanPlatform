"""UC05 ``publishCityChanges``: drain the C06 outbox to the event bus.

Delivery is at least once. An event is marked as published only after the
broker confirms it. If the broker is down, events stay in the outbox and go
out on a later pass (UC05 AF6). The same holds while no consumer queue is bound
for the event type yet (for example C06 starting before C07 on a fresh broker):
the broker returns the event instead of dropping it, so it is never marked
published unseen. If a confirmation is received but the mark is lost, the event
is sent again with the same ``eventId``; consumers deduplicate on it, as the
event contract requires.

Run as a worker::

    DATABASE_URL=... RABBITMQ_URL=... uv run python -m \\
        app.components.c06_city_monitoring.publisher
"""

import os

from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.shared.publisher import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EXCHANGE,
    DEFAULT_INTERVAL_SECONDS,
    DrainReport,
    EventPublisher,
    PublishError,
    RabbitMQPublisher,
    drain_outbox,
    run,
    serve,
)

# Existing component clients import the publishing API here.
__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EXCHANGE",
    "DEFAULT_INTERVAL_SECONDS",
    "DrainReport",
    "EventPublisher",
    "PublishError",
    "RabbitMQPublisher",
    "drain_outbox",
    "run",
]


def main() -> None:  # pragma: no cover - process entry point
    serve("C06", PostgresCityStateStore(os.environ["DATABASE_URL"]))


if __name__ == "__main__":  # pragma: no cover
    main()
