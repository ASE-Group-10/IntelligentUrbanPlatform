"""Consume C05 PlanReady messages from the shared topic exchange."""

import logging
import os
import signal
import threading

from opentelemetry.trace import get_current_span
from pydantic import ValidationError

from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.events import DEFAULT_EXCHANGE, EventConsumer
from app.shared.logging import configure_logging, trace
from app.shared.topology import SUBSCRIBER_QUEUES

from .events import PlanReadyEvent
from .store import AllocationStore
from .telemetry import C08Telemetry

QUEUE = "c08.plan-ready"
log = logging.getLogger(__name__)


def make_consumer(
    store, url, exchange=DEFAULT_EXCHANGE, queue=QUEUE, *, telemetry=None
):
    def handle(envelope):
        try:
            event = PlanReadyEvent.model_validate_json(
                envelope.model_dump_json(by_alias=True)
            )
        except ValidationError:
            if telemetry is not None:
                get_current_span().set_attribute("outcome", "invalid")
            log.warning("Invalid PlanReady", extra=trace(envelope))
            return "reject"
        result = store.consume(event)
        if telemetry is not None:
            get_current_span().set_attribute("outcome", result)
        level = (
            logging.WARNING
            if result
            in {
                "ineligible vehicle",
                "ineligible driver",
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

    def observed(envelope):
        return telemetry.consume(envelope, handle)

    return EventConsumer(
        url,
        queue,
        list(SUBSCRIBER_QUEUES[QUEUE]),
        observed if telemetry is not None else handle,
        exchange,
    )


def main():
    configure_logging("C08")
    settings = get_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    telemetry = C08Telemetry(endpoint) if endpoint else None
    try:
        consumer = make_consumer(
            AllocationStore(engine, settings.availability_max_age_seconds),
            os.environ["RABBITMQ_URL"],
            os.getenv("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
            telemetry=telemetry,
        )
        try:
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            consumer.run(stop)
        finally:
            consumer.close()
    finally:
        try:
            if telemetry is not None:
                telemetry.shutdown()
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
