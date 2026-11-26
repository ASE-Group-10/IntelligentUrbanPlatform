"""Local synthetic C04 fixture -> RabbitMQ -> C11/C12 -> saved replay demo.

No real C04 service or complete simulation is claimed. Refuses remote services.
Run against a fresh local database/broker with C11/C12 migrations applied.
"""

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select

from app.components.analytics_admin import uuid4 as uuid4_argument
from app.components.analytics_verify import LocalAPI, verify_http
from app.components.c06_city_monitoring.publisher import RabbitMQPublisher
from app.components.c11_kpi_analytics.store import observations
from app.components.c11_kpi_analytics.worker import make_consumer as c11_consumer
from app.components.c12_simulation_replay.recording import CaptureRecorder, captures
from app.components.c12_simulation_replay.worker import make_consumer as c12_consumer
from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.contracts import EventEnvelope
from app.shared.events import DEFAULT_EXCHANGE
from app.shared.logging import configure_logging


def fixture_events(run_id, delivery_id, correlation_id, now, provider_id):
    """Two labelled synthetic outcomes, one trace and an explicit causal chain."""
    folder = (
        Path(__file__).resolve().parents[3]
        / "contracts/components/c04-delivery-management/examples"
    )
    events = []
    for version, name in enumerate(
        ["delivery-submitted-event.json", "delivery-progress-changed-event.json"], 1
    ):
        raw = json.loads((folder / name).read_text())
        when = now - timedelta(minutes=1) if version == 1 else now
        raw.update(
            eventId=str(uuid4()),
            correlationId=str(correlation_id),
            causationId=str(events[-1].event_id) if events else None,
            scope="simulation",
            runId=str(run_id),
            observedAt=when.isoformat(),
            emittedAt=when.isoformat(),
            entityVersions={str(delivery_id): version},
        )
        payload = raw["payload"]
        payload.update(
            deliveryId=str(delivery_id),
            deliveryVersion=version,
            providerId=str(provider_id),
        )
        payload["delivery"].update(deliveryId=str(delivery_id), version=version)
        payload["delivery"]["progress"]["updatedAt"] = when.isoformat()
        events.append(EventEnvelope.model_validate(raw))
    return events


def wait_for_recording(engine, run_id, events, timeout=20):
    """Fail if either independently running subscriber missed an outcome."""
    expected = {event.event_id for event in events}
    deadline = time.monotonic() + timeout
    while True:
        with engine.connect() as conn:
            observed = set(
                conn.execute(
                    select(observations.c.event_id).where(
                        observations.c.event_id.in_(expected)
                    )
                ).scalars()
            )
            body = conn.execute(
                select(captures.c.body).where(captures.c.run_id == run_id)
            ).scalar_one()
        if body.get("invalid"):
            raise RuntimeError("C12 rejected an outcome; capture cannot be verified")
        recorded = [EventEnvelope.model_validate(raw) for raw in body["events"]]
        if observed == expected and recorded == events:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("C11/C12 worker evidence missing; capture not sealed")
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-id", type=uuid4_argument, required=True)
    parser.add_argument("--run-id", type=uuid4_argument, required=True)
    parser.add_argument("--correlation-id", type=uuid4_argument, default=None)
    parser.add_argument("--external-workers", action="store_true")
    parser.add_argument(
        "--api-url", help="Also verify authorised HTTP reads and playback (local only)"
    )
    args = parser.parse_args()
    if args.api_url and not args.external_workers:
        parser.error("--api-url requires --external-workers")
    correlation_id = args.correlation_id or uuid4()
    api = (
        LocalAPI(
            args.api_url,
            os.environ.get("ANALYTICS_DEMO_TOKEN", ""),
            args.provider_id,
            correlation_id,
        )
        if args.api_url
        else None
    )
    configure_logging("C11-C12-synthetic-demo")
    engine = make_engine(get_settings().database_url.get_secret_value())
    broker = os.environ["RABBITMQ_URL"]
    local = {"localhost", "127.0.0.1", "db", "rabbitmq"}
    if engine.url.host not in local or urlparse(broker).hostname not in local:
        raise ValueError("Synthetic demo requires local PostgreSQL and RabbitMQ")
    now = datetime.now(UTC)
    capture = CaptureRecorder(engine)
    capture.begin(
        args.run_id,
        args.provider_id,
        now - timedelta(minutes=2),
        {
            "initialState": {"deliveries": {}},
            "source": "labelled synthetic C04 examples",
        },
        True,
    )
    subscribers = [c11_consumer(engine, broker), c12_consumer(engine, broker)]
    publisher = RabbitMQPublisher(
        broker, exchange=os.environ.get("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE)
    )
    delivery_id = uuid4()
    events = fixture_events(
        args.run_id, delivery_id, correlation_id, now, args.provider_id
    )
    try:
        for subscriber in subscribers:
            subscriber.declare()
        for message in events:
            publisher.publish(message)
        if not args.external_workers:
            for subscriber in subscribers:
                subscriber.consume_available()
        wait_for_recording(engine, args.run_id, events)
        capture.seal(args.run_id, now + timedelta(minutes=1))
        if api:
            verify_http(api, args.provider_id, args.run_id, events, now)
        print("Synthetic C04-only recording saved (incomplete platform coverage).")
        print(f"runId: {args.run_id}")
        print(f"providerId: {args.provider_id}")
        print(f"correlationId: {correlation_id}")
        print("HTTP verification: passed" if api else "HTTP verification: not run")
        print(
            f"UTC query range: {now - timedelta(minutes=2)} "
            f"to {now + timedelta(minutes=1)}"
        )
    finally:
        publisher.close()
        for subscriber in subscribers:
            subscriber.close()
        engine.dispose()


if __name__ == "__main__":
    main()
