"""Generate a Dublin proposal from a saved C04 delivery JSON object on stdin.

From backend/ with the local database migrated and the C09 demo corpus loaded:

    python -m app.demo.c05 < saved-delivery.json

The default trace and run come from fixtures/dublin-scenario.json. Pass the
original delivery trace and run when replaying a different simulation.
"""

import argparse
import asyncio
import json
import sys
from urllib.parse import urlencode
from uuid import UUID

import psycopg
from pydantic import UUID4, TypeAdapter

from app.components.c04_delivery_management.planning import seed_demo_delivery
from app.components.c05_planning.models import Plan, PlanScope, SavedDelivery
from app.components.c05_planning.service import PlanningService
from app.components.c05_planning.store import GenerationConflictError, PostgresPlanStore
from app.components.c06_city_monitoring.router import libpq_url
from app.main import app
from app.shared.result import Failure, Ok, Result


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
        conn.commit()  # C04 has saved this delivery before calling C05.
        # Keep confirmation from changing the delivery between the read and the
        # in-process generation call. Re-importing a fixture never rewinds C04.
        (current,) = conn.execute(
            """select delivery from c04_delivery_management.deliveries
                where run_id = %s and delivery_id = %s for share""",
            (scope.run_id, delivery.delivery_id),
        ).fetchone()
        if SavedDelivery.model_validate(current) == delivery:
            generated = planning.generate_plan(delivery, scope, trace_id, key)
            if isinstance(generated, Failure):
                return generated

        # After a confirmation the original generation receipt can still be
        # replayed, while a new key has no receipt and cannot create a stale plan.
        store = PostgresPlanStore(dsn)
        try:
            prior = store.replay(
                conn,
                scope.scope,
                scope.run_id,
                "service:c04",
                key,
                delivery.model_dump(mode="json", by_alias=True),
            )
        except GenerationConflictError:
            return Failure("Conflict", "C05.IDEMPOTENCY_MISMATCH")
        if prior is None:
            return Failure("Conflict", "C04.DELIVERY_CHANGED")
        plan, original_trace = prior
        return Ok((Plan.model_validate(plan, strict=False), original_trace))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-id", type=TypeAdapter(UUID4).validate_python)
    parser.add_argument("--correlation-id", type=TypeAdapter(UUID4).validate_python)
    parser.add_argument(
        "--key", help="Generation key (defaults to delivery ID/version)"
    )
    args = parser.parse_args()
    try:
        saved = json.load(sys.stdin.buffer)
    except ValueError as exc:
        parser.error(f"Invalid saved delivery: {exc}")

    async with app.router.lifespan_context(app):
        scenario = app.state.dublin_scenario
        scope = PlanScope(scope="simulation", run_id=args.run_id or scenario.run_id)
        trace_id = args.correlation_id or scenario.correlation_id
        dsn = libpq_url(app.state.settings.database_url.get_secret_value())
        try:
            result = generate_saved_demo(
                app.state.planning, dsn, scope, saved, trace_id, args.key
            )
        except ValueError as exc:
            parser.error(str(exc))
    if isinstance(result, Failure):
        print(f"{result.kind}: {result.message}", file=sys.stderr)
        return 1
    plan, original_trace = result.value
    print(plan.model_dump_json(by_alias=True))
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
