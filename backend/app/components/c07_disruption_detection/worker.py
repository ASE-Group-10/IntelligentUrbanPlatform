"""C07 worker: consume ``CityStateUpdated`` and run ``evaluateChange`` (#73).

Walking-skeleton hop 8. Disruptions are kept in Postgres
(``PostgresDisruptionStore``), so they survive a restart and the API can read
them. The C05 planning port reads the confirmed plan and saves its revision.

Run from ``backend/``::

    DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop?sslmode=disable \\
    RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F \\
        uv run python -m app.components.c07_disruption_detection.worker
"""

import logging
import os
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from app.components.c05_planning.replanning import make_replanning
from app.components.c07_disruption_detection.detection import DisruptionDetection
from app.components.c07_disruption_detection.postgres_store import (
    PostgresDisruptionStore,
)
from app.components.c07_disruption_detection.telemetry import C07Telemetry
from app.shared.contracts import EventEnvelope
from app.shared.events import DEFAULT_EXCHANGE, Disposition, EventConsumer, EventHandler
from app.shared.logging import configure_logging, trace
from app.shared.result import Ok
from app.shared.topology import SUBSCRIBER_QUEUES

QUEUE = "c07.city-state-updated"
EVENT_TYPES = list(SUBSCRIBER_QUEUES[QUEUE])

log = logging.getLogger(__name__)


def c07_handler(
    detection: DisruptionDetection, *, telemetry: C07Telemetry | None = None
) -> EventHandler:
    """Map ``evaluate_change`` results onto ack / retry / reject.

    An outcome C07 could not finish because C05 answered ``Unavailable`` (the
    lookup, or some replan requests) has ``retryable`` set and is retried, so the
    event comes back once C05 answers. Everything else is acked, including plans
    C05 will not take (``NotFound``, a repeated ``Conflict``): delivering the same
    event again would give the same answer.
    """

    def handle(envelope: EventEnvelope) -> Disposition:
        result = (
            telemetry.evaluate(envelope, detection.evaluate_change)
            if telemetry is not None
            else detection.evaluate_change(envelope)
        )
        if isinstance(result, Ok):
            outcome = result.value
            log.log(
                logging.WARNING if outcome.retryable else logging.INFO,
                "evaluated: %s, %d replan request(s)%s%s",
                outcome.kind,
                len(outcome.replan_requests),
                f" ({outcome.reason})" if outcome.reason else "",
                "; will retry" if outcome.retryable else "",
                extra=trace(envelope),
            )
            return "retry" if outcome.retryable else "ack"
        log.warning("%s: %s", result.kind, result.message, extra=trace(envelope))
        return "reject" if result.kind == "Invalid" else "retry"

    return handle


def make_consumer(
    detection: DisruptionDetection,
    url: str,
    exchange: str = DEFAULT_EXCHANGE,
    queue: str = QUEUE,
    *,
    telemetry: C07Telemetry | None = None,
) -> EventConsumer:
    return EventConsumer(
        url,
        queue=queue,
        event_types=EVENT_TYPES,
        handler=c07_handler(detection, telemetry=telemetry),
        exchange=exchange,
    )


def make_detection(
    conninfo: str, *, clock: Callable[[], datetime] | None = None
) -> DisruptionDetection:
    now = clock or (lambda: datetime.now(UTC))
    return DisruptionDetection(
        planning=make_replanning(conninfo, clock=now),
        store=PostgresDisruptionStore(conninfo),
        clock=now,
    )


def main() -> None:  # pragma: no cover - process entry point
    configure_logging("C07")
    detection = make_detection(os.environ["DATABASE_URL"])
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    telemetry = C07Telemetry(endpoint) if endpoint else None
    try:
        consumer = make_consumer(
            detection,
            os.environ["RABBITMQ_URL"],
            os.environ.get("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
            telemetry=telemetry,
        )
        try:
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            log.info("C07 worker started (C05 planning, Postgres store)")
            consumer.run(stop)
        finally:
            consumer.close()
    finally:
        if telemetry is not None:
            telemetry.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
