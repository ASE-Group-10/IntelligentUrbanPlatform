"""Receive C04 observations through the shared durable RabbitMQ consumer."""

import os
import signal
import threading

from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.events import DEFAULT_EXCHANGE, EventConsumer
from app.shared.logging import configure_logging
from app.shared.topology import SUBSCRIBER_QUEUES

from .store import ObservationStore

QUEUE = "c11.delivery-observations"


def make_consumer(engine, url):
    return EventConsumer(
        url,
        QUEUE,
        list(SUBSCRIBER_QUEUES[QUEUE]),
        ObservationStore(engine).consume,
        exchange=os.environ.get("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
    )


def main():
    configure_logging("C11")
    engine = make_engine(get_settings().database_url.get_secret_value())
    consumer = make_consumer(engine, os.environ["RABBITMQ_URL"])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        consumer.run(stop)
    finally:
        consumer.close()
        engine.dispose()


if __name__ == "__main__":
    main()
