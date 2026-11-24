"""Publish a confirmed plan twice for the hop-6 demo."""

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
from app.demo.feed import load_scenario
from app.infrastructure.database import make_engine
from app.shared.events import DEFAULT_EXCHANGE

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "contracts/components/c08-allocation/examples/plan-ready-event.json"
)


C05_EXAMPLES = FIXTURE.parents[2] / "c05-planning/examples"


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


def shared_plan():
    """Build the initial event for C05's saved v3 plan in the Dublin run."""
    scenario = load_scenario()
    confirmed = json.loads((C05_EXAMPLES / "confirmed-plan.json").read_text())
    data = json.loads(FIXTURE.read_text())
    plan = confirmed["planId"]
    version = confirmed["version"]
    data.update(
        # Stable event identity, including when the demo publisher is restarted.
        eventId="abc22c21-5710-4819-91cd-fb7d17577175",
        correlationId=str(scenario.correlation_id),
        observedAt=scenario.start_time.isoformat(),
        emittedAt=scenario.start_time.isoformat(),
        **confirmed["runScope"],
        entityVersions={plan: version},
        affected={"planIds": [plan], "resourceIds": [], "segmentKeys": []},
        payload={
            "planId": plan,
            "planVersion": version,
            "decisionId": confirmed["decisionId"],
            "requirements": confirmed["resourceNeeds"],
        },
    )
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
        saved = conn.execute(
            text("""
            INSERT INTO c08_allocation.demo_plans
                (run_id,plan_id,plan_version,delivery_id,provider_id,resource_id,driver_id,required_kg)
            VALUES (:run,:plan,:version,:delivery,:provider,:resource,:driver,:kg)
            ON CONFLICT (run_id,plan_id) DO NOTHING
            RETURNING plan_id
        """),
            {
                "run": event.run_id,
                "plan": event.payload.plan_id,
                "version": event.payload.plan_version,
                "delivery": need.delivery_id,
                "provider": provider,
                "resource": resource,
                "driver": driver,
                "kg": need.capacity.amount,
            },
        ).scalar()
        if saved is None:
            previous = conn.execute(
                text("""
                    SELECT plan_version, delivery_id, provider_id, resource_id,
                           driver_id, required_kg
                    FROM c08_allocation.demo_plans
                    WHERE plan_id=:plan AND run_id IS NOT DISTINCT FROM :run
                """),
                {"plan": event.payload.plan_id, "run": event.run_id},
            ).one()
            if previous != (
                event.payload.plan_version,
                need.delivery_id,
                provider,
                resource,
                driver,
                need.capacity.amount,
            ):
                raise ValueError(
                    "This plan already has a different saved demo configuration."
                )


def save_demo_revision(store, saved, departure: datetime):
    """Save the demo route and its planned departure as one confirmed version."""
    if saved["status"] != "confirmed":
        raise ValueError("The saved plan must be confirmed.")
    if departure.utcoffset() is None:
        raise ValueError("Planned departure must have a UTC offset.")
    with store.engine.begin() as conn:
        current = conn.execute(
            text("""
            UPDATE c08_allocation.demo_plans
            SET plan_version=:version, saved_revision=CAST(:saved AS jsonb),
                departure_at=:departure
            WHERE plan_id=:plan AND run_id IS NOT DISTINCT FROM :run AND
                (plan_version < :version OR
                 (plan_version = :version AND saved_revision=CAST(:saved AS jsonb)
                  AND departure_at IS NOT DISTINCT FROM :departure))
            RETURNING plan_id
        """),
            {
                "version": saved["version"],
                "saved": json.dumps(saved),
                "departure": departure,
                "plan": saved["planId"],
                "run": saved["runScope"].get("runId"),
            },
        ).scalar()
        if current is None:
            raise ValueError(
                "Seed the initial plan first. A saved version cannot be replaced."
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("provider", "resource", "driver"):
        parser.add_argument(f"--{name}", type=UUID)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--revision", action="store_true", help="Save the Dublin detour and publish v4."
    )
    source.add_argument(
        "--live", action="store_true", help="Publish a fresh standalone live plan."
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Prepare the saved-plan lookup and let C05 publish the event.",
    )
    args = parser.parse_args()
    if not args.revision and not all((args.provider, args.resource, args.driver)):
        parser.error(
            "--provider, --resource and --driver are required for the initial plan"
        )
    settings = get_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    store = AllocationStore(engine, settings.availability_max_age_seconds)
    try:
        if args.revision:
            event = PlanReadyEvent.model_validate_json(
                (C05_EXAMPLES / "plan-ready-revision-event.json").read_text()
            )
            saved = json.loads((C05_EXAMPLES / "revised-plan.json").read_text())
            save_demo_revision(store, saved, load_scenario().start_time)
        else:
            event = new_plan() if args.live else shared_plan()
            seed_plan(store, event, args.provider, args.resource, args.driver)
        if args.seed_only:
            print(f"Prepared planId={event.payload.plan_id} runId={event.run_id}")
            return
        publisher = RabbitMQPublisher(
            os.environ["RABBITMQ_URL"],
            os.getenv("RABBITMQ_EXCHANGE", DEFAULT_EXCHANGE),
            {QUEUE: ["PlanReady"]},
        )
        try:
            publisher.publish(event)
            publisher.publish(event)
        finally:
            publisher.close()
        print(
            f"Published PlanReady twice: eventId={event.event_id} "
            f"scope={event.scope} runId={event.run_id} "
            f"planId={event.payload.plan_id} correlationId={event.correlation_id}"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
