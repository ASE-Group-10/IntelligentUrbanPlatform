"""Generate a Dublin proposal from a saved C04 delivery JSON object on stdin.

From backend/ with the local database migrated and the C09 demo corpus loaded:

    python -m app.demo.c05 < saved-delivery.json

The default trace and run come from fixtures/dublin-scenario.json. Pass the
original delivery trace and run when replaying a different simulation.
"""

import argparse
import asyncio
import sys
from uuid import UUID

from pydantic import UUID4, TypeAdapter, ValidationError

from app.components.c05_planning.models import PlanScope, SavedDelivery
from app.main import app
from app.shared.result import Failure


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-id", type=TypeAdapter(UUID4).validate_python)
    parser.add_argument("--correlation-id", type=UUID)
    parser.add_argument(
        "--key", help="Generation key (defaults to delivery ID/version)"
    )
    args = parser.parse_args()
    try:
        delivery = SavedDelivery.model_validate_json(sys.stdin.buffer.read())
    except ValidationError as exc:
        parser.error(f"Invalid saved delivery: {exc}")

    async with app.router.lifespan_context(app):
        scenario = app.state.dublin_scenario
        scope = PlanScope(scope="simulation", run_id=args.run_id or scenario.run_id)
        key = (
            f"c04:generate:{delivery.delivery_id}:v{delivery.version}"
            if args.key is None
            else args.key
        )
        result = app.state.planning.generate_plan(
            delivery, scope, args.correlation_id or scenario.correlation_id, key
        )
    if isinstance(result, Failure):
        print(f"{result.kind}: {result.message}", file=sys.stderr)
        return 1
    print(result.value.model_dump_json(by_alias=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
