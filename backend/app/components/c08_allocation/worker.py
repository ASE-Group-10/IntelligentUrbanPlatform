"""Consume C05 PlanReady messages from the shared topic exchange."""

import logging
import os
import signal
import threading

from pydantic import ValidationError

from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.events import DEFAULT_EXCHANGE, EventConsumer
from app.shared.logging import configure_logging, trace
from app.shared.topology import SUBSCRIBER_QUEUES

from .events import PlanReadyEvent
from .store import AllocationStore

QUEUE = "c08.plan-ready"
log = logging.getLogger(__name__)


def make_consumer(store, url, exchange=DEFAULT_EXCHANGE, queue=QUEUE):
    def handle(envelope):
        try:
            event = PlanReadyEvent.model_validate_json(
                envelope.model_dump_json(by_alias=True)
            )
        except ValidationError:
            log.warning("Invalid PlanReady", extra=trace(envelope))
            return "reject"
        result = store.consume(event)
        level = (
            logging.WARNING
            if result
            in {
                "ineligible vehicle",
                "insufficient capacity",
                "unresolved",
                "event differs from saved plan",
                "invalid saved delivery route",
                "incompatible route",
            }
            else logging.INFO
        )
        log.log(level, "PlanReady: %s", result, extra=trace(event))
        return "retry" if result == "plan unavailable" else "ack"

    return EventConsumer(url, queue, list(SUBSCRIBER_QUEUES[QUEUE]), handle, exchange)


def main():
    configure_logging("C08")
    settings = get_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    consumer = make_consumer(
        AllocationStore(engine, settings.availability_max_age_seconds),
        os.environ["RABBITMQ_URL"],
        os.getenv("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
    )
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
