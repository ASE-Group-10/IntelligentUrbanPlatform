"""Persist explicitly registered C04 simulation captures from RabbitMQ."""

import os
import signal
import threading

from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.events import EventConsumer
from app.shared.logging import configure_logging
from app.shared.topology import SUBSCRIBER_QUEUES

from .recording import CaptureRecorder

QUEUE = "c12.delivery-recordings"


def make_consumer(engine, url):
    return EventConsumer(
        url, QUEUE, list(SUBSCRIBER_QUEUES[QUEUE]), CaptureRecorder(engine).consume
    )


def main():
    configure_logging("C12")
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
