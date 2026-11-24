"""Generate a Dublin proposal from a saved C04 delivery JSON object on stdin.

From backend/ with the local database migrated and the C09 demo corpus loaded:

    python -m app.demo.c05 < saved-delivery.json

Add --fresh for a new synthetic delivery with a current UTC window. The
default run and trace come from fixtures/dublin-scenario.json.
The local Compose demo-confirm service confirms an imported proposal by ID.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import psycopg
from pydantic import UUID4, TypeAdapter

from app.components.c04_delivery_management.planning import seed_demo_delivery
from app.components.c05_planning.models import Plan, PlanScope, SavedDelivery
from app.components.c05_planning.service import PlanningService
from app.components.c06_city_monitoring.router import libpq_url
from app.config import get_settings
from app.demo.c05_confirmation import confirm_demo_plan
from app.demo.feed import load_scenario
from app.main import app
from app.shared.logging import configure_logging
from app.shared.result import Failure, Result


def fresh_delivery(saved: dict, now: datetime) -> dict:
    """Create a new pending demo delivery with time for confirmation and the closure."""
    template = SavedDelivery.model_validate(saved)
    if (
        template.version != 1
        or template.status != "pending"
        or template.progress.status != "pending"
        or saved.get("plannedAt") is not None
    ):
        raise ValueError("--fresh requires a new, pending delivery example")
    if now.utcoffset() is None:
        raise ValueError("The demo clock must provide a timezone-aware timestamp")
    now = now.astimezone(UTC)
    submitted = now.isoformat().replace("+00:00", "Z")
    duration = template.time_window.end - template.time_window.start
    try:
        departure = now + timedelta(minutes=30)
        end = departure + duration
    except OverflowError as exc:
        raise ValueError("The fresh delivery window extends beyond year 9999") from exc
    return {
        **saved,
        "deliveryId": str(uuid4()),
        "createdAt": submitted,
        "updatedAt": submitted,
        "progress": {**saved["progress"], "updatedAt": submitted},
        "timeWindow": {
            "start": departure.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        },
    }


def generate_saved_demo(
    planning: PlanningService,
    dsn: str,
    scope: PlanScope,
    saved: dict,
    trace_id: UUID,
    key: str | None = None,
) -> Result[tuple[Plan, UUID]]:
    """Replay the original command or generate from current, unchanged C04 facts."""
    delivery = SavedDelivery.model_validate(saved)
    if key is None:
        key = f"c04:generate:{delivery.delivery_id}:v{delivery.version}"
    with psycopg.connect(dsn) as conn:
        seed_demo_delivery(conn, scope.run_id, saved)
    return planning.generate_with_trace(delivery, scope, trace_id, key)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-id", type=TypeAdapter(UUID4).validate_python)
    parser.add_argument("--correlation-id", type=TypeAdapter(UUID4).validate_python)
    generation = parser.add_mutually_exclusive_group()
    generation.add_argument(
        "--key", help="Generation key (defaults to delivery ID/version)"
    )
    generation.add_argument(
        "--fresh",
        action="store_true",
        help="create a new pending example with departure in 30 minutes",
    )
    generation.add_argument(
        "--confirm",
        metavar="PLAN_ID",
        type=TypeAdapter(UUID4).validate_python,
        help="confirm an imported proposal using the local Compose fixture operator",
    )
    args = parser.parse_args()
    if args.confirm is not None:
        configure_logging("C05")
        settings = get_settings()
        scope = PlanScope(
            scope="simulation", run_id=args.run_id or load_scenario().run_id
        )
        result = confirm_demo_plan(
            libpq_url(settings.database_url.get_secret_value()),
            scope,
            settings.shared_run_ids,
            args.confirm,
            args.correlation_id,
        )
    else:
        try:
            saved = json.load(sys.stdin.buffer)
            if args.fresh:
                saved = fresh_delivery(saved, datetime.now(UTC))
            async with app.router.lifespan_context(app):
                scenario = app.state.dublin_scenario
                scope = PlanScope(
                    scope="simulation", run_id=args.run_id or scenario.run_id
                )
                result = generate_saved_demo(
                    app.state.planning,
                    libpq_url(app.state.settings.database_url.get_secret_value()),
                    scope,
                    saved,
                    args.correlation_id or scenario.correlation_id,
                    args.key,
                )
        except ValueError as exc:
            parser.error(f"Invalid saved delivery: {exc}")
    if isinstance(result, Failure):
        print(f"{result.kind}: {result.message}", file=sys.stderr)
        return 1
    plan, original_trace = result.value
    print(plan.model_dump_json(by_alias=True))
    if args.confirm is not None:
        print(f"Local fixture confirmation receipt: v{plan.version}", file=sys.stderr)
    query = urlencode(
        {
            "planId": plan.plan_id,
            "scope": scope.scope,
            "runId": scope.run_id,
            "correlationId": original_trace,
        }
    )
    print(f"Plans page: /plans?{query}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
