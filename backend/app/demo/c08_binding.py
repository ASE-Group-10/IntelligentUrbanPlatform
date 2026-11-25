"""Bind a generated Dublin proposal to a local demo van and driver (hop 6).

The local Compose ``demo-bind`` service runs this before ``demo-confirm``::

    docker compose run --rm demo-bind "$plan" --run-id "$run"

It creates one fixture provider, operator, driver and C03 van (idempotent),
then binds them to the proposal through C08's own ``bind_c05_proposal``. C08
then allocates the van when C05 publishes ``PlanReady``. The driver is a
fixture identity and cannot sign in, so use ``app.demo.c08 --c05-plan`` with
real C02 users when the driver should accept the offer in Plans.

It connects only to the fixed loopback database inside the db container's
network namespace and ignores DATABASE_URL, like ``c12-local-setup``.
"""

import argparse
import os
import sys
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app.components.c02_identity_access import records
from app.components.c05_planning.store import PostgresPlanStore
from app.components.c08_allocation.store import AllocationStore
from app.components.resources.schemas import resource_adapter
from app.components.resources.service import ResourceService
from app.config import get_settings
from app.demo.c08 import bind_c05_proposal
from app.demo.feed import load_scenario
from app.infrastructure.database import make_engine
from app.integrations.mocks import MockAgentGateway, MockLocationGateway
from app.shared.auth import AuthContext

LOCAL_DSN = "postgresql+psycopg://ulop:ulop@127.0.0.1:5432/ulop"
LOCAL_LIBPQ = "postgres://ulop:ulop@127.0.0.1:5432/ulop?sslmode=disable"


def _fixture_id(name):
    return uuid5(NAMESPACE_URL, f"urn:ulop:local-demo:c08:{name}")


PROVIDER_ID = _fixture_id("provider")
OPERATOR_ID = _fixture_id("operator")
DRIVER_ID = _fixture_id("driver")
VAN_KEY = "local-demo-c08-van"
VAN = {
    "external_identifier": "LOCAL-DEMO-VAN",
    "name": "Local demo van",
    "resource_type": "vehicle",
    "address": "Dublin Depot (synthetic)",
    "latitude": 53.3498,
    "longitude": -6.2603,
    "observed_availability": "available",
    "details": {
        "vehicle_category": "van",
        "max_weight_kg": 800,
        "powertrain": "electric",
        "autonomous": False,
    },
}


def seed_fixture(engine, freshness_seconds):
    """Create the fixture provider, members and van once; return the van ID."""
    with engine.begin() as conn:
        conn.execute(
            insert(records.providers)
            .values(provider_id=PROVIDER_ID, provider_name="Local demo provider")
            .on_conflict_do_nothing()
        )
        for principal, name, roles in (
            (OPERATOR_ID, "Local demo operator", ("manager", "operator")),
            (DRIVER_ID, "Local demo driver", ("driver",)),
        ):
            conn.execute(
                insert(records.profiles)
                .values(principal_id=principal, display_name=name, platform_admin=False)
                .on_conflict_do_nothing()
            )
            for role in roles:
                conn.execute(
                    insert(records.memberships)
                    .values(principal_id=principal, provider_id=PROVIDER_ID, role=role)
                    .on_conflict_do_nothing()
                )
    context = AuthContext(
        principal_id=OPERATOR_ID,
        principal_type="user",
        acting_provider_id=PROVIDER_ID,
        roles=("manager", "operator"),
        provider_ids=(PROVIDER_ID,),
        platform_roles=(),
        correlation_id=uuid4(),
    )
    # A fixed idempotency key replays the same van on every later run.
    _, van, _ = ResourceService(engine, freshness_seconds).create(
        context,
        resource_adapter.validate_python(VAN),
        VAN_KEY,
        str(context.correlation_id),
        MockLocationGateway("matched"),
        MockAgentGateway("active"),
    )
    return UUID(van["id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan_id", type=UUID, help="proposal from app.demo.c05")
    parser.add_argument("--run-id", type=UUID, help="default: Dublin fixture run")
    args = parser.parse_args(argv)
    if os.environ.get("C08_LOCAL_DEMO") != "1":
        parser.error("Use the local Compose demo-bind service")
    run_id = args.run_id or load_scenario().run_id
    settings = get_settings()
    engine = make_engine(LOCAL_DSN)
    try:
        with engine.connect() as conn:
            if conn.execute(text("select current_database()")).scalar() != "ulop":
                parser.error("demo-bind only runs against the local Compose database")
        van = seed_fixture(engine, settings.availability_max_age_seconds)
        store = AllocationStore(engine, settings.availability_max_age_seconds)
        try:
            bind_c05_proposal(
                store,
                PostgresPlanStore(LOCAL_LIBPQ),
                run_id,
                args.plan_id,
                PROVIDER_ID,
                van,
                DRIVER_ID,
            )
        except ValueError as error:
            print(f"demo-bind: {error}", file=sys.stderr)
            if "eligible" in str(error):
                print(
                    "demo-bind: the fixture van's availability is observed once and "
                    "goes stale after availability_max_age_seconds. Recreate the "
                    "local volume (`docker compose down -v`) to reset it.",
                    file=sys.stderr,
                )
            return 1
    finally:
        engine.dispose()
    print(
        f"Bound local demo van {van} and driver {DRIVER_ID} to planId="
        f"{args.plan_id} runId={run_id}. Confirm the proposal next."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
