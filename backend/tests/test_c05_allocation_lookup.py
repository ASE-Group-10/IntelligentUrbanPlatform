"""C08 reads route facts and rechecks a version in its own PostgreSQL transaction."""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.components.c05_planning.allocation import (
    check_current_version,
    get_delivery_route,
)
from app.components.c05_planning.models import Plan, PlanScope
from app.components.c05_planning.store import lock_plan
from app.shared.result import Failure, Ok
from tests.c05_flow import flow as flow


@pytest.fixture
def confirmed(flow):
    response = flow.confirm()
    assert response.status_code == 200, response.text
    return Plan.model_validate_json(response.content)


def seed_revision(writer, confirmed, flow, example):
    """Persist a later snapshot to exercise reads independently of C07/hop 8."""
    lock_plan(writer, confirmed.run_scope, confirmed.plan_id)
    revised = confirmed.model_dump(mode="json", by_alias=True)
    revised["version"] += 1
    revised["decisionId"] = str(uuid4())
    route = example("revised-plan.json")["routes"][0]
    route["deliveryIds"] = revised["requestIds"].copy()
    # Make the requested delivery's detour the second route in this revision.
    other_id = str(uuid4())
    revised["requestIds"].append(other_id)
    revised["routes"] = [
        {**revised["routes"][0], "deliveryIds": [other_id]},
        route,
    ]
    revised["resourceNeeds"].append(
        {**revised["resourceNeeds"][0], "deliveryId": other_id}
    )
    departure = datetime.fromisoformat(
        example("delivery-submitted.json")["timeWindow"]["start"]
    )
    departure += timedelta(minutes=11)
    writer.execute(
        "insert into c05_planning.plan_versions "
        "(proposal, correlation_id, planned_departure_at) values (%s, %s, %s)",
        (Jsonb(revised), flow.trace, departure),
    )
    return Plan.model_validate(revised, strict=False), departure


def test_only_confirmed_snapshots_in_the_requested_scope_are_visible(flow):
    plan, engine = flow.plan, flow.client.app.state.engine
    scope, plan_id, delivery_id = plan.run_scope, plan.plan_id, plan.request_ids[0]
    with engine.begin() as reader:
        result = get_delivery_route(reader, scope, plan_id, 1, delivery_id)
        assert isinstance(result, Failure) and result.kind == "NotFound"
        pending = check_current_version(reader, scope, plan_id, 1)
        assert isinstance(pending, Failure) and pending.kind == "Conflict"

    assert flow.confirm().status_code == 200
    other_run = PlanScope(scope="simulation", run_id=uuid4())
    with engine.begin() as reader:
        for missing in (
            (scope, plan_id, 1, delivery_id),
            (scope, plan_id, 3, delivery_id),
            (scope, plan_id, 2, uuid4()),
            (scope, uuid4(), 2, delivery_id),
            (other_run, plan_id, 2, delivery_id),
            (PlanScope(scope="live"), plan_id, 2, delivery_id),
        ):
            result = get_delivery_route(reader, *missing)
            assert isinstance(result, Failure) and result.kind == "NotFound", missing
        other = check_current_version(reader, other_run, plan_id, 2)
        assert isinstance(other, Failure) and other.kind == "NotFound"


def test_route_and_departure_are_from_the_exact_committed_version(
    flow, confirmed, example, test_database_url
):
    with psycopg.connect(test_database_url) as writer:
        revised, revised_departure = seed_revision(writer, confirmed, flow, example)
    scope, plan_id = confirmed.run_scope, confirmed.plan_id
    delivery_id = confirmed.request_ids[0]
    first_departure = datetime.fromisoformat(
        example("delivery-submitted.json")["timeWindow"]["start"]
    )
    assert first_departure != revised_departure
    assert confirmed.routes[0].segment_keys != revised.routes[1].segment_keys
    with flow.client.app.state.engine.begin() as reader:
        for snapshot, route, departure in (
            (confirmed, confirmed.routes[0], first_departure),
            (revised, revised.routes[1], revised_departure),
        ):
            result = get_delivery_route(
                reader, scope, plan_id, snapshot.version, delivery_id
            )
            assert isinstance(result, Ok), result
            assert result.value.plan == snapshot
            assert result.value.route == route
            assert result.value.planned_departure_at == departure
            assert result.value.planned_departure_at.utcoffset() is not None
        stale = check_current_version(reader, scope, plan_id, confirmed.version)
        assert isinstance(stale, Failure) and stale.kind == "Conflict"
        assert check_current_version(reader, scope, plan_id, revised.version) == Ok(
            None
        )


def test_commit_version_check_holds_the_writer_until_allocation_finishes(
    flow, confirmed, test_database_url
):
    scope, plan_id = confirmed.run_scope, confirmed.plan_id
    with psycopg.connect(test_database_url) as writer:
        with flow.client.app.state.engine.begin() as allocation:
            assert check_current_version(
                allocation, scope, plan_id, confirmed.version
            ) == Ok(None)
            with pytest.raises(psycopg.errors.LockNotAvailable), writer.transaction():
                writer.execute("set local lock_timeout = '250ms'")
                lock_plan(writer, scope, plan_id)
        with writer.transaction():
            writer.execute("set local lock_timeout = '2s'")
            lock_plan(writer, scope, plan_id)


def test_commit_version_check_waits_for_pending_revision_then_sees_it(
    flow, confirmed, example, test_database_url
):
    scope, plan_id = confirmed.run_scope, confirmed.plan_id
    reader_pid = Future()

    def recheck():
        with flow.client.app.state.engine.begin() as allocation:
            reader_pid.set_result(
                allocation.exec_driver_sql("select pg_backend_pid()").scalar_one()
            )
            return check_current_version(allocation, scope, plan_id, confirmed.version)

    with ThreadPoolExecutor(1) as pool, psycopg.connect(test_database_url) as writer:
        with writer.transaction():
            seed_revision(writer, confirmed, flow, example)
            outcome = pool.submit(recheck)
            pid = reader_pid.result(timeout=5)
            deadline = monotonic() + 5
            while not writer.execute(
                "select pg_backend_pid() = any(pg_blocking_pids(%s))", (pid,)
            ).fetchone()[0]:
                assert not outcome.done(), "allocation bypassed the pending revision"
                assert monotonic() < deadline, "allocation did not wait for the writer"
                sleep(0.01)
        result = outcome.result(timeout=5)
        assert isinstance(result, Failure) and result.kind == "Conflict"


def test_current_check_requires_a_read_committed_postgres_transaction(flow):
    plan = flow.plan
    engine = flow.client.app.state.engine

    def check(conn):
        result = check_current_version(conn, plan.run_scope, plan.plan_id, plan.version)
        assert isinstance(result, Failure) and result.kind == "Unavailable"

    with engine.connect() as conn:
        check(conn)
    for isolation in ("AUTOCOMMIT", "REPEATABLE READ"):
        with (
            engine.connect().execution_options(isolation_level=isolation) as conn,
            conn.begin(),
        ):
            check(conn)
