"""Publish one synthetic confirmed live plan twice for the hop-6 demo."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from app.components.c06_city_monitoring.publisher import RabbitMQPublisher
from app.components.c08_allocation.events import PlanReadyEvent
from app.components.c08_allocation.store import AllocationStore
from app.components.c08_allocation.worker import QUEUE
from app.config import get_settings
from app.infrastructure.database import make_engine
from app.shared.events import DEFAULT_EXCHANGE

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "contracts/components/c08-allocation/examples/plan-ready-event.json"
)


def new_plan():
    data = json.loads(FIXTURE.read_text())
    plan = str(uuid4())
    data.update(
        eventId=str(uuid4()),
        correlationId=str(uuid4()),
        observedAt=datetime.now(UTC).isoformat(),
        emittedAt=datetime.now(UTC).isoformat(),
    )
    data["affected"]["planIds"] = [plan]
    data["entityVersions"] = {plan: data["payload"]["planVersion"]}
    data["payload"].update(planId=plan, decisionId=str(uuid4()))
    data["payload"]["requirements"][0]["deliveryId"] = str(uuid4())
    return PlanReadyEvent.model_validate_json(json.dumps(data))


def seed_plan(store, event, provider, resource, driver):
    need = event.payload.requirements[0]
    with store.engine.begin() as conn:
        eligible = store.vehicle(conn, provider, resource)
        if (
            not eligible
            or eligible["details"]["vehicle_category"] != need.resource_kind
        ):
            raise ValueError("Select an eligible C03 van for this provider.")
        member = conn.execute(
            text("""
            SELECT 1 FROM c02_identity_access.provider_memberships
            WHERE provider_id=:provider AND principal_id=:driver AND role='driver'
        """),
            {"provider": provider, "driver": driver},
        ).scalar()
        if not member:
            raise ValueError(
                "The intended driver needs a C02 driver membership for this provider."
            )
        conn.execute(
            text("""
            INSERT INTO c08_allocation.demo_plans
                (plan_id,plan_version,delivery_id,provider_id,resource_id,driver_id,required_kg)
            VALUES (:plan,:version,:delivery,:provider,:resource,:driver,:kg)
        """),
            {
                "plan": event.payload.plan_id,
                "version": event.payload.plan_version,
                "delivery": need.delivery_id,
                "provider": provider,
                "resource": resource,
                "driver": driver,
                "kg": need.capacity.amount,
            },
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("provider", "resource", "driver"):
        parser.add_argument(f"--{name}", type=UUID, required=True)
    args = parser.parse_args()
    settings = get_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    publisher = RabbitMQPublisher(
        os.environ["RABBITMQ_URL"],
        os.getenv("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
        {QUEUE: ["PlanReady"]},
    )
    event = new_plan()
    try:
        seed_plan(
            AllocationStore(engine, settings.availability_max_age_seconds),
            event,
            args.provider,
            args.resource,
            args.driver,
        )
        publisher.publish(event)
        publisher.publish(event)
        print(
            f"Published PlanReady twice: eventId={event.event_id} "
            f"planId={event.payload.plan_id} correlationId={event.correlation_id}"
        )
    finally:
        publisher.close()
        engine.dispose()


if __name__ == "__main__":
    main()
