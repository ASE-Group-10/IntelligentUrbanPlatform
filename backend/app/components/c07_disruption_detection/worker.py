"""C07 worker: consume ``CityStateUpdated`` and run ``evaluateChange`` (#73).

Walking-skeleton hop 8. Until C05 and the C07 Postgres store exist it uses the
labelled C05 stub (``planning_stub.py``) and the in-memory disruption store.

Run from ``backend/``::

    RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F \\
        uv run python -m app.components.c07_disruption_detection.worker
"""

import logging
import os
import signal
import threading
from datetime import UTC, datetime

from app.components.c07_disruption_detection.detection import DisruptionDetection
from app.components.c07_disruption_detection.planning_stub import DemoPlanningStub
from app.components.c07_disruption_detection.store import InMemoryDisruptionStore
from app.shared.contracts import EventEnvelope
from app.shared.events import DEFAULT_EXCHANGE, Disposition, EventConsumer, EventHandler
from app.shared.logging import configure_logging, trace
from app.shared.result import Ok
from app.shared.topology import SUBSCRIBER_QUEUES

QUEUE = "c07.city-state-updated"
EVENT_TYPES = list(SUBSCRIBER_QUEUES[QUEUE])

log = logging.getLogger(__name__)


def c07_handler(detection: DisruptionDetection) -> EventHandler:
    """Map ``evaluate_change`` results onto ack / retry / reject.

    An outcome C07 could not finish because C05 was down (the lookup, or some
    replan requests) is retried, so the event comes back once C05 answers.
    Other ``unknown`` outcomes are acked: delivering the same event again would
    give the same answer.
    """

    def handle(envelope: EventEnvelope) -> Disposition:
        result = detection.evaluate_change(envelope)
        if isinstance(result, Ok):
            outcome = result.value
            log.log(
                logging.WARNING if outcome.retry_later else logging.INFO,
                "evaluated: %s, %d replan request(s)%s%s",
                outcome.kind,
                len(outcome.replan_requests),
                f" ({outcome.reason})" if outcome.reason else "",
                "; will retry" if outcome.retry_later else "",
                extra=trace(envelope),
            )
            return "retry" if outcome.retry_later else "ack"
        log.warning("%s: %s", result.kind, result.message, extra=trace(envelope))
        return "reject" if result.kind == "Invalid" else "retry"

    return handle


def make_consumer(
    detection: DisruptionDetection,
    url: str,
    exchange: str = DEFAULT_EXCHANGE,
    queue: str = QUEUE,
) -> EventConsumer:
    return EventConsumer(
        url,
        queue=queue,
        event_types=EVENT_TYPES,
        handler=c07_handler(detection),
        exchange=exchange,
    )


def main() -> None:  # pragma: no cover - process entry point
    configure_logging("C07")
    detection = DisruptionDetection(
        planning=DemoPlanningStub(),
        store=InMemoryDisruptionStore(),
        clock=lambda: datetime.now(UTC),
    )
    consumer = make_consumer(
        detection,
        os.environ["RABBITMQ_URL"],
        os.environ.get("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("C07 worker started (C05 stub, in-memory store)")
    try:
        consumer.run(stop)
    finally:
        consumer.close()


if __name__ == "__main__":  # pragma: no cover
    main()
