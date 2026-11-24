"""Local CLI confirmation follows the persisted Dublin proposal and its trace."""

import asyncio
import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from io import BytesIO, TextIOWrapper
from unittest.mock import Mock
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app.components.c05_planning.models import PlanScope
from app.components.c05_planning.replanning import make_replanning
from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c07_disruption_detection.contracts import (
    ReplanRequest,
    ReplanTrigger,
)
from app.demo import c05, c05_confirmation
from app.demo.feed import feed_closure, load_scenario
from app.shared.result import Failure, Ok
from tests.c05_flow import flow as flow


@pytest.fixture
def demo_dsn(test_database_url, monkeypatch):
    url = make_url(test_database_url).set(drivername="postgresql")
    if url.host == "localhost":
        url = url.set(host="127.0.0.1")
    monkeypatch.setenv("C05_LOCAL_DEMO", "1")
    with psycopg.connect(test_database_url) as conn:
        peers = conn.execute("select inet_client_addr(), inet_server_addr()").fetchone()
    # GitHub's dedicated test service sees Docker bridge peers on its mapped port.
    if any(peer is None or not peer.is_loopback for peer in peers):
        monkeypatch.setattr(c05_confirmation, "_check_server", lambda *args: None)
    return url.render_as_string(hide_password=False)


def test_fresh_cli_proposal_confirms_once_even_after_a_revision(
    flow, demo_dsn, c06_database, example, monkeypatch, capsys
):
    run_id, trace = flow.plan.run_scope.run_id, uuid4()
    url = make_url(demo_dsn).set(drivername="postgresql+psycopg")
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("SHARED_RUN_IDS", json.dumps([str(run_id)]))

    def command(*args):
        monkeypatch.setattr(sys, "argv", ["c05", "--run-id", str(run_id), *args])
        result = asyncio.run(c05.main())
        output = capsys.readouterr()
        assert result == 0, output.err
        return json.loads(output.out)

    stdin = TextIOWrapper(
        BytesIO(json.dumps(example("delivery-submitted.json")).encode())
    )
    monkeypatch.setattr(sys, "stdin", stdin)
    proposal = command("--fresh", "--correlation-id", str(trace))
    plan_id = UUID(proposal["planId"])
    assert (
        plan_id != flow.plan.plan_id and proposal["status"] == "awaiting_confirmation"
    )
    # stdin is exhausted; confirmation must read this exact proposal from C05.
    confirmed = command("--confirm", str(plan_id))
    assert confirmed == {**proposal, "version": 2, "status": "confirmed"}
    assert (
        command("--confirm", str(plan_id), "--correlation-id", str(trace)) == confirmed
    )

    def clock():
        return datetime.now(UTC)

    report = feed_closure(
        PostgresCityStateStore(c06_database),
        load_scenario(),
        clock,
        fresh=True,
        run_id=run_id,
        correlation_id=trace,
    )
    assert isinstance(report, Ok), report
    trigger = ReplanTrigger(
        disruption_id=uuid4(),
        disruption_version=1,
        based_on_plan_version=2,
        reason="road_closed",
        city_snapshot_version=report.value.snapshot_version,
        source_event_id=report.value.queued_event_id,
    )
    revision = make_replanning(c06_database, clock=clock).request_replan(
        ReplanRequest.build(plan_id, trigger, scope="simulation", run_id=run_id), trace
    )
    assert isinstance(revision, Ok) and revision.value.new_plan_version == 3, revision
    assert command("--confirm", str(plan_id)) == confirmed

    with psycopg.connect(c06_database) as conn:
        assert conn.execute(
            "select delivery->>'status', (delivery->>'version')::int, plan_id "
            "from c04_delivery_management.deliveries "
            "where run_id=%s and delivery_id=%s",
            (run_id, UUID(proposal["requestIds"][0])),
        ).fetchone() == ("planned", 2, plan_id)
        assert conn.execute(
            "select version, envelope->>'correlationId' from c05_planning.outbox "
            "where plan_id=%s order by version",
            (plan_id,),
        ).fetchall() == [(2, str(trace)), (3, str(trace))]
        assert conn.execute(
            "select principal_id, provider_id, version "
            "from c05_planning.confirmation_receipts where plan_id=%s",
            (plan_id,),
        ).fetchall() == [
            (
                c05_confirmation.LOCAL_DEMO_OPERATOR_ID,
                c05_confirmation.LOCAL_DEMO_PROVIDER_ID,
                2,
            )
        ]
        assert conn.execute(
            "select operation, principal_id, outcome "
            "from c10_decision_records.audit_outcomes where correlation_id=%s "
            "order by operation",
            (trace,),
        ).fetchall() == [
            ("C04.markRequestsPlanned", "service:c05", "Ok"),
            ("C05.confirmPlan", str(c05_confirmation.LOCAL_DEMO_OPERATOR_ID), "Ok"),
        ]
        assert conn.execute(
            "select version from c05_planning.plan_versions where plan_id=%s",
            (flow.plan.plan_id,),
        ).fetchall() == [(1,)]
        assert conn.execute(
            "select (select count(*) from c02_identity_access.profiles "
            "where principal_id=%s), (select count(*) "
            "from c02_identity_access.providers where provider_id=%s)",
            (
                c05_confirmation.LOCAL_DEMO_OPERATOR_ID,
                c05_confirmation.LOCAL_DEMO_PROVIDER_ID,
            ),
        ).fetchone() == (0, 0)


@pytest.mark.parametrize(
    ("opt_in", "dsn"),
    [
        ("0", "postgresql://ulop@127.0.0.1/ulop"),
        ("1", "postgresql://ulop@localhost/ulop"),
        ("1", "postgresql://ulop@db.example.com/ulop_test"),
        ("1", "postgresql://ulop@127.0.0.1/production"),
        ("1", "postgresql://another_role@127.0.0.1/ulop"),
        ("1", "postgresql://ulop@127.0.0.1/ulop_test?host=db.example.com"),
        ("1", "postgresql://ulop@127.0.0.1/ulop_test?service=forwarded"),
    ],
)
def test_demo_confirmation_refuses_untrusted_targets_before_connect(
    monkeypatch, opt_in, dsn
):
    monkeypatch.setenv("C05_LOCAL_DEMO", opt_in)
    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **kw: pytest.fail("unexpected connection")
    )
    scope = PlanScope(scope="simulation", run_id=uuid4())
    result = c05_confirmation.confirm_demo_plan(
        dsn, scope, frozenset({scope.run_id}), uuid4()
    )
    assert isinstance(result, Failure)


@pytest.mark.parametrize(
    "database, role, server, client",
    [
        ("ulop_test", "ulop", "192.0.2.42", "127.0.0.1"),
        ("ulop_test", "ulop", "127.0.0.1", "192.0.2.42"),
        ("another_db", "ulop", "127.0.0.1", "127.0.0.1"),
        ("ulop_test", "another_role", "127.0.0.1", "127.0.0.1"),
    ],
)
def test_connected_database_is_verified_before_reading_a_proposal(
    monkeypatch, database, role, server, client
):
    monkeypatch.setenv("C05_LOCAL_DEMO", "1")
    conn = Mock()
    conn.info.user = "ulop"
    conn.execute.return_value.fetchone.return_value = (database, role, server, client)
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: nullcontext(conn))
    scope = PlanScope(scope="simulation", run_id=uuid4())
    result = c05_confirmation.confirm_demo_plan(
        "postgresql://ulop@127.0.0.1/ulop_test",
        scope,
        frozenset({scope.run_id}),
        uuid4(),
    )
    assert isinstance(result, Failure) and result.kind == "Forbidden"
    conn.execute.assert_called_once()
    assert "inet_server_addr()" in conn.execute.call_args.args[0]


def test_demo_confirmation_refuses_wrong_run_or_trace_without_writing(
    flow, test_database_url, demo_dsn
):
    scope, plan_id = flow.plan.run_scope, flow.plan.plan_id
    other = PlanScope(scope="simulation", run_id=uuid4())
    for selected, shared, trace in [
        (scope, frozenset(), flow.trace),
        (other, frozenset({other.run_id}), flow.trace),
        (scope, frozenset({scope.run_id}), uuid4()),
    ]:
        result = c05_confirmation.confirm_demo_plan(
            demo_dsn,
            selected,
            shared,
            plan_id,
            trace,
        )
        assert isinstance(result, Failure)
    with psycopg.connect(test_database_url) as conn:
        assert conn.execute(
            "select version from c05_planning.plan_versions where plan_id=%s",
            (plan_id,),
        ).fetchall() == [(1,)]
        assert conn.execute(
            "select count(*) from c05_planning.outbox where plan_id=%s", (plan_id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "select delivery->>'status', plan_id from "
            "c04_delivery_management.deliveries where run_id=%s and delivery_id=%s",
            (scope.run_id, flow.plan.request_ids[0]),
        ).fetchone() == ("pending", None)
