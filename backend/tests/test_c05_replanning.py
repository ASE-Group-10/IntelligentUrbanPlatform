"""UC07 from a committed Dublin plan through C06, C07 and the revised PlanReady."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from itertools import count
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pika
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app.components.c05_planning.allocation import (
    check_current_version,
    get_delivery_route,
)
from app.components.c05_planning.publisher import PostgresPlanOutbox
from app.components.c05_planning.replanning import make_replanning
from app.components.c05_planning.store import PostgresPlanStore
from app.components.c06_city_monitoring.contracts import Observation, ObservationTarget
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c06_city_monitoring.store import StateScope
from app.components.c07_disruption_detection.contracts import (
    ChangeScope,
    ReplanRequest,
    ReplanTrigger,
)
from app.components.c07_disruption_detection.postgres_store import (
    PostgresDisruptionStore,
)
from app.components.c07_disruption_detection.worker import make_consumer, make_detection
from app.components.c08_allocation.events import PlanReadyEvent
from app.components.c08_allocation.worker import make_consumer as allocation_consumer
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c10_decision_records.service import DecisionRecords
from app.demo.c08 import bind_c05_proposal
from app.demo.feed import feed_closure, load_scenario
from app.shared.contracts import EventEnvelope
from app.shared.publisher import RabbitMQPublisher, drain_outbox
from app.shared.result import Failure, Ok
from tests.c05_flow import flow as flow
from tests.test_c08_driver_acceptance import headers
from tests.test_c08_driver_acceptance import scenario as vehicle_scenario
from tests.test_resources import PAYLOAD

c08 = vehicle_scenario


@pytest.fixture
def scenario():
    return load_scenario()


@pytest.fixture
def clock(scenario):
    # C04's delivery window and the C06 observation use this simulation's date.
    return lambda: scenario.closure.observed_at + timedelta(seconds=5)


@pytest.fixture
def city(c06_database):
    return PostgresCityStateStore(c06_database)


@pytest.fixture
def planning(test_database_url, clock):
    return make_replanning(test_database_url, clock=clock)


def inject_closure(city, scenario, clock, flow):
    report = feed_closure(
        city,
        scenario,
        clock,
        run_id=flow.plan.run_scope.run_id,
        correlation_id=flow.trace,
    )
    assert isinstance(report, Ok), report
    assert report.value.disposition == "merged"
    assert report.value.queued_event_id is not None
    return report.value


@pytest.fixture
def replan(flow, city, scenario, clock):
    assert flow.confirm().status_code == 200
    report = inject_closure(city, scenario, clock, flow)
    return ReplanRequest.build(
        flow.plan.plan_id,
        ReplanTrigger(
            disruption_id=uuid4(),
            disruption_version=1,
            based_on_plan_version=2,
            reason="road_closed",
            city_snapshot_version=report.snapshot_version,
            source_event_id=report.queued_event_id,
        ),
        scope="simulation",
        run_id=report.run_id,
    )


def db_counts(conn, plan_id):
    return conn.execute(
        """select
          (select count(*) from c05_planning.plan_versions where plan_id=%(id)s),
          (select count(*) from c10_decision_records.decisions where subject_id=%(id)s),
          (select count(*) from c05_planning.outbox where plan_id=%(id)s),
          (select count(*) from c05_planning.replan_receipts where plan_id=%(id)s)""",
        {"id": plan_id},
    ).fetchone()


def test_concurrent_retries_and_rediscovery_reuse_one_committed_revision(
    flow, planning, replan, test_database_url
):
    gate = Barrier(2)

    def send():
        gate.wait(timeout=5)
        return planning.request_replan(replan, flow.trace)

    with ThreadPoolExecutor(2) as pool:
        first, second = [pool.submit(send) for _ in range(2)]
        result = first.result(timeout=15)
        assert second.result(timeout=15) == result
    assert isinstance(result, Ok) and result.value.status == "PlanSaved", result
    assert result.value.new_plan_version == 3
    changed = ReplanRequest.build(
        replan.plan_id,
        replan.trigger.model_copy(update={"reason": "weather"}),
        replan.scope,
        replan.run_id,
    )
    assert changed.key == replan.key
    misuse = planning.request_replan(changed, flow.trace)
    assert isinstance(misuse, Failure) and misuse.kind == "Invalid"
    rediscovered = ReplanRequest.build(
        replan.plan_id,
        replan.trigger.model_copy(update={"based_on_plan_version": 3}),
        replan.scope,
        replan.run_id,
    )
    assert rediscovered.key != replan.key
    assert planning.request_replan(rediscovered, flow.trace) == result
    with psycopg.connect(test_database_url) as conn:
        assert db_counts(conn, replan.plan_id)[:3] == (3, 2, 2)


@pytest.mark.parametrize("failure_at", ["decision", "outbox"])
def test_failed_revision_rolls_back_and_same_request_can_then_succeed(
    flow, planning, replan, test_database_url, monkeypatch, failure_at
):
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        before = db_counts(observer, replan.plan_id)
        assert before == (2, 1, 1, 0)
        if failure_at == "decision":
            record = DecisionRecords.record_decision

            def refuse_after_write(self, conn, ctx, entry):
                assert isinstance(record(self, conn, ctx, entry), Ok)
                assert db_counts(conn, replan.plan_id)[1] == 2
                assert db_counts(observer, replan.plan_id) == before
                return Failure("Unavailable", "injected after C10 insert")

            with monkeypatch.context() as patch:
                patch.setattr(DecisionRecords, "record_decision", refuse_after_write)
                failure = planning.request_replan(replan, flow.trace)
        else:
            observer.execute(
                "alter table c05_planning.outbox add constraint test_no_revision_event "
                "check (version < 3) not valid"
            )
            try:
                failure = planning.request_replan(replan, flow.trace)
            finally:
                observer.execute(
                    "alter table c05_planning.outbox "
                    "drop constraint test_no_revision_event"
                )
        assert isinstance(failure, Failure) and failure.kind == "Unavailable", failure
        assert db_counts(observer, replan.plan_id) == before
        retry = planning.request_replan(replan, flow.trace)
        assert isinstance(retry, Ok) and retry.value.status == "PlanSaved", retry
        assert db_counts(observer, replan.plan_id) == (3, 2, 2, 1)


def close_detour(city, scenario, clock, replan, trace):
    detour = next(s for s in scenario.segments if s.name == "Abbey Street Lower")
    return CityMonitoring(city, clock).ingest_observation(
        SourceContext(StateScope("simulation", replan.run_id), trace),
        Observation(
            observation_id=uuid4(),
            source_id=scenario.closure.source_id,
            kind="closure",
            target=ObservationTarget(segment_key=detour.segment_key),
            values={"status": "closed", "cause": "roadworks"},
            observed_at=clock(),
        ),
    )


def test_detour_closed_during_policy_query_prevents_stale_revision(
    flow, planning, replan, scenario, clock, test_database_url, monkeypatch
):
    query = planning.knowledge.query_knowledge
    city = PostgresCityStateStore(
        make_conninfo(test_database_url, options="-c lock_timeout=2s")
    )

    def close_while_evaluating(ctx, policy):
        answer = query(ctx, policy)
        assert isinstance(answer, Ok), answer
        closed = close_detour(city, scenario, clock, replan, flow.trace)
        assert isinstance(closed, Ok), closed
        assert closed.value.snapshot_version == replan.trigger.city_snapshot_version + 1
        return answer

    with monkeypatch.context() as patch:
        patch.setattr(planning.knowledge, "query_knowledge", close_while_evaluating)
        outdated = planning.request_replan(replan, flow.trace)
    assert isinstance(outdated, Failure) and outdated.kind == "Unavailable", outdated
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        assert db_counts(observer, replan.plan_id) == (2, 1, 1, 0)
        retry = planning.request_replan(replan, flow.trace)
        assert isinstance(retry, Ok) and retry.value.status == "ReviewRequired", retry
        assert db_counts(observer, replan.plan_id) == (2, 2, 1, 1)


def test_c06_write_waits_for_committing_revision_and_decision_follows_evidence(
    flow, replan, scenario, clock, test_database_url, monkeypatch
):
    ticks = count()
    planning = make_replanning(
        test_database_url, clock=lambda: clock() + timedelta(seconds=next(ticks))
    )
    name = f"c06-closure-{uuid4()}"
    city = PostgresCityStateStore(
        make_conninfo(
            test_database_url, application_name=name, options="-c lock_timeout=10s"
        )
    )
    ready = Event()
    record = planning.decisions.record_decision

    def write_closure():
        assert ready.wait(timeout=5)
        return close_detour(city, scenario, clock, replan, flow.trace)

    with psycopg.connect(test_database_url, autocommit=True) as observer:
        with ThreadPoolExecutor(1) as pool:
            writer = pool.submit(write_closure)

            def record_while_writer_waits(conn, ctx, entry):
                ready.set()
                # Identify the actual C06 backend waiting on this C05 transaction.
                deadline = monotonic() + 5
                while not observer.execute(
                    """select 1 from pg_stat_activity where application_name=%s
                           and %s=any(pg_blocking_pids(pid))""",
                    (name, conn.info.backend_pid),
                ).fetchone():
                    assert not writer.done(), "the C06 write overtook the commit"
                    assert monotonic() < deadline, "C06 never waited for the C05 writer"
                    sleep(0.01)
                return record(conn, ctx, entry)

            monkeypatch.setattr(
                planning.decisions, "record_decision", record_while_writer_waits
            )
            try:
                saved = planning.request_replan(replan, flow.trace)
                assert isinstance(saved, Ok) and saved.value.status == "PlanSaved", (
                    saved
                )
            finally:
                ready.set()
            closed = writer.result(timeout=5)
            assert isinstance(closed, Ok), closed
            assert (
                closed.value.snapshot_version
                == replan.trigger.city_snapshot_version + 1
            )

        decision, event = observer.execute(
            """select d.record, o.envelope from c10_decision_records.decisions d
                 join c05_planning.outbox o
                   on o.plan_id = d.subject_id and o.version = d.subject_version
                where d.decision_id=%s""",
            (saved.value.decision_id,),
        ).fetchone()
    assert (
        decision["inputVersions"]["citySnapshotVersion"]
        == replan.trigger.city_snapshot_version
    )
    retrieved = max(
        datetime.fromisoformat(s["retrievedAt"]) for s in decision["evidenceSnapshots"]
    )
    decided = datetime.fromisoformat(decision["decidedAt"])
    observed = datetime.fromisoformat(event["observedAt"])
    assert (
        retrieved <= decided <= observed <= datetime.fromisoformat(event["emittedAt"])
    )


def test_discovery_and_replan_refresh_actual_delivery_and_run(
    flow, planning, replan, scenario, test_database_url
):
    plan = flow.plan
    changed = ChangeScope(
        scope="simulation",
        run_id=replan.run_id,
        segment_keys=[scenario.closure.segment_key],
    )
    work = planning.find_affected_work(changed)
    assert isinstance(work, Ok), work
    (item,) = work.value.items
    assert (item.plan_id, item.plan_version, item.delivery_ids) == (
        plan.plan_id,
        2,
        plan.request_ids,
    )
    other_run = changed.model_copy(update={"run_id": uuid4()})
    assert planning.find_affected_work(other_run).value.items == []
    wrong_request = replan.model_copy(update={"run_id": other_run.run_id})
    missing = planning.request_replan(wrong_request, flow.trace)
    assert isinstance(missing, Failure) and missing.kind == "NotFound"
    stale = ReplanRequest.build(
        plan.plan_id,
        replan.trigger.model_copy(update={"based_on_plan_version": 1}),
        replan.scope,
        replan.run_id,
    )
    conflict = planning.request_replan(stale, flow.trace)
    assert isinstance(conflict, Failure) and conflict.kind == "Conflict"
    (delivery_id,) = plan.request_ids
    with psycopg.connect(test_database_url, autocommit=True) as conn:
        updated = conn.execute(
            """update c04_delivery_management.deliveries
                  set status='delivered', version=3, delivery=jsonb_set(
                      delivery || '{"status":"delivered","version":3}',
                      '{progress,status}', '"delivered"')
                where run_id=%s and delivery_id=%s""",
            (replan.run_id, delivery_id),
        )
        assert updated.rowcount == 1
        assert planning.find_affected_work(changed).value.items == []
        same_plan = planning.request_replan(replan, flow.trace)
        assert isinstance(same_plan, Ok) and same_plan.value.status == "NoChange", (
            same_plan
        )
        versions, _, events, _ = db_counts(conn, plan.plan_id)
        assert (versions, events) == (2, 1)


def test_discovery_defaults_to_the_run_clock_but_honours_historical_windows(
    flow, scenario, c07_database, city
):
    assert flow.confirm().status_code == 200
    plan = flow.plan
    with flow.client.app.state.engine.begin() as conn:
        saved = get_delivery_route(
            conn, plan.run_scope, plan.plan_id, 2, plan.request_ids[0]
        )
    assert isinstance(saved, Ok)
    departure = saved.value.planned_departure_at
    arrival = departure + timedelta(
        seconds=saved.value.route.estimated_duration_seconds
    )
    planning = make_replanning(c07_database, clock=lambda: arrival)
    change = ChangeScope(
        scope="simulation",
        run_id=plan.run_scope.run_id,
        segment_keys=[scenario.closure.segment_key],
    )

    historical = change.model_copy(update={"from_": departure, "to": arrival})
    work = planning.find_affected_work(historical)
    assert isinstance(work, Ok) and [item.plan_id for item in work.value.items] == [
        plan.plan_id
    ]
    explicit = planning.find_affected_work(change.model_copy(update={"from_": arrival}))
    assert isinstance(explicit, Ok) and not explicit.value.items
    assert planning.find_affected_work(change) == explicit
    assert planning.find_affected_work(
        change.model_copy(update={"to": departure})
    ) == Failure("Invalid", "C05.INVALID_CHANGE_WINDOW")

    # C07 supplies only the changed segment and run when a real closure arrives.
    closure = feed_closure(
        city,
        scenario,
        lambda: arrival,
        fresh=True,
        run_id=change.run_id,
        correlation_id=flow.trace,
    )
    assert isinstance(closure, Ok)
    (event,) = city.state(StateScope("simulation", change.run_id)).outbox
    outcome = make_detection(c07_database, clock=lambda: arrival).evaluate_change(event)
    assert isinstance(outcome, Ok) and outcome.value.kind == "no_disruption"
    assert not outcome.value.replan_requests


def observe_road(
    city, clock, flow, scenario, name, status, observed_at, *, kind="closure", **values
):
    segment = next(s for s in scenario.segments if s.name == name)
    scope = StateScope(scope="simulation", run_id=flow.plan.run_scope.run_id)
    result = CityMonitoring(city, clock).ingest_observation(
        SourceContext(scope, flow.trace),
        Observation(
            observation_id=uuid4(),
            source_id=scenario.closure.source_id,
            kind=kind,
            target=ObservationTarget(segment_key=segment.segment_key),
            values={"status": status, "cause": "roadworks", **values},
            observed_at=observed_at,
        ),
    )
    assert isinstance(result, Ok), result
    return result.value.snapshot_version


@pytest.mark.parametrize(
    ("segment_name", "new_status", "outcome"),
    [
        ("Eden Quay", "open", "NoChange"),
        ("Abbey Street Lower", "closed", "ReviewRequired"),
        ("Abbey Street Lower", "open", "PlanSaved"),
    ],
)
def test_later_city_snapshot_changes_the_replan_outcome(
    flow,
    planning,
    replan,
    city,
    scenario,
    clock,
    test_database_url,
    segment_name,
    new_status,
    outcome,
):
    version = observe_road(
        city, clock, flow, scenario, segment_name, new_status, clock()
    )
    assert version > replan.trigger.city_snapshot_version
    result = planning.request_replan(replan, flow.trace)
    assert isinstance(result, Ok) and result.value.status == outcome, result
    if outcome == "ReviewRequired":
        assert result.value.reason
    assert planning.request_replan(replan, flow.trace) == result
    with psycopg.connect(test_database_url) as conn:
        versions, _, events, _ = db_counts(conn, replan.plan_id)
        if outcome == "PlanSaved":
            assert (versions, events) == (3, 2)
            (record,) = conn.execute(
                "select record from c10_decision_records.decisions "
                "where decision_id=%s",
                (result.value.decision_id,),
            ).fetchone()
            assert record["inputVersions"]["citySnapshotVersion"] == version
        else:
            assert (versions, events) == (2, 1)


@pytest.mark.parametrize(
    "segments",
    [("Eden Quay",), ("Abbey Street Lower",), ("Eden Quay", "Abbey Street Lower")],
)
def test_future_open_reports_cannot_clear_or_divert_the_route(
    flow, planning, replan, city, scenario, clock, segments, test_database_url
):
    for name in segments:
        observe_road(
            city, clock, flow, scenario, name, "open", clock() + timedelta(hours=2)
        )
    result = planning.request_replan(replan, flow.trace)
    assert isinstance(result, Ok) and result.value.status == "ReviewRequired", result
    with psycopg.connect(test_database_url) as conn:
        assert db_counts(conn, replan.plan_id) == (2, 2, 1, 1)


@pytest.mark.parametrize(
    ("minutes_open", "outcome"), [(4, "ReviewRequired"), (8, "PlanSaved")]
)
def test_explicit_open_report_must_cover_the_eight_minute_detour(
    flow, planning, replan, city, scenario, clock, minutes_open, outcome
):
    with flow.client.app.state.engine.begin() as conn:
        saved = get_delivery_route(
            conn, flow.plan.run_scope, replan.plan_id, 2, flow.plan.request_ids[0]
        )
    assert isinstance(saved, Ok), saved
    expiry = saved.value.planned_departure_at + timedelta(minutes=minutes_open)
    observe_road(
        city,
        clock,
        flow,
        scenario,
        "Abbey Street Lower",
        "open",
        clock(),
        validUntil=expiry.isoformat(),
    )
    result = planning.request_replan(replan, flow.trace)
    assert isinstance(result, Ok) and result.value.status == outcome, result


@pytest.mark.parametrize(
    ("seconds_until_expiry", "outcome"),
    [(-1, "ReviewRequired"), (60, "ReviewRequired"), (7200, "NoChange")],
)
def test_reopening_must_still_be_valid_for_the_original_plan(
    flow, planning, replan, city, scenario, clock, seconds_until_expiry, outcome
):
    observe_road(
        city,
        clock,
        flow,
        scenario,
        "Eden Quay",
        "open",
        clock() - timedelta(seconds=2),
        validUntil=(clock() + timedelta(seconds=seconds_until_expiry)).isoformat(),
    )
    result = planning.request_replan(replan, flow.trace)
    assert isinstance(result, Ok) and result.value.status == outcome, result


@pytest.mark.parametrize(
    ("traffic", "outcome"),
    [
        ({"delaySeconds": 180, "speedKmh": 25}, "ReviewRequired"),
        ({"speedKmh": 0}, "ReviewRequired"),
        ({"speedKmh": 6}, "ReviewRequired"),
        ({"speedKmh": 12}, "PlanSaved"),
        ({"speedKmh": 25, "delaySeconds": 0}, "PlanSaved"),
    ],
)
def test_reported_traffic_must_support_the_fixed_detour(
    flow, planning, replan, city, scenario, clock, traffic, outcome
):
    observe_road(
        city,
        clock,
        flow,
        scenario,
        "Abbey Street Lower",
        "open",
        clock(),
        kind="traffic",
        cause="traffic",
        **traffic,
    )
    result = planning.request_replan(replan, flow.trace)
    assert isinstance(result, Ok) and result.value.status == outcome, result


@pytest.mark.parametrize(
    "limit,delay,expected",
    [
        ("window", 2, "PlanSaved"),
        ("window", 6, "ReviewRequired"),
        ("closure", 5, "ReviewRequired"),
        ("detour", 6, "ReviewRequired"),
        ("freshness", 901, "ReviewRequired"),
    ],
)
def test_policy_lookup_time_counts_towards_remaining_delivery_window(
    flow,
    replan,
    city,
    scenario,
    test_database_url,
    monkeypatch,
    example,
    limit,
    delay,
    expected,
):
    end = datetime.fromisoformat(
        example("delivery-submitted.json")["timeWindow"]["end"]
    )
    now = (
        end - timedelta(minutes=8, seconds=5)
        if limit == "window"
        else end - timedelta(hours=1)
    )
    planning = make_replanning(test_database_url, clock=lambda: now)
    fresh = feed_closure(
        city,
        scenario,
        lambda: now,
        fresh=True,
        run_id=replan.run_id,
        correlation_id=flow.trace,
    )
    assert isinstance(fresh, Ok)
    if limit in ("closure", "detour"):
        is_closure = limit == "closure"
        expiry = (
            now
            + timedelta(seconds=5)
            + (timedelta() if is_closure else timedelta(minutes=8))
        )
        observe_road(
            city,
            lambda: now,
            flow,
            scenario,
            "Eden Quay" if is_closure else "Abbey Street Lower",
            "closed" if is_closure else "open",
            now,
            validUntil=expiry.isoformat(),
        )
    request = ReplanRequest.build(
        replan.plan_id,
        replan.trigger.model_copy(
            update={
                "source_event_id": fresh.value.queued_event_id,
                "city_snapshot_version": fresh.value.snapshot_version,
            }
        ),
        replan.scope,
        replan.run_id,
    )
    query = planning.knowledge.query_knowledge

    def query_after_delay(ctx, policy):
        nonlocal now
        now += timedelta(seconds=delay)
        return query(ctx, policy)

    monkeypatch.setattr(planning.knowledge, "query_knowledge", query_after_delay)
    result = planning.request_replan(request, flow.trace)
    assert isinstance(result, Ok) and result.value.status == expected, result
    with psycopg.connect(test_database_url) as conn:
        if expected == "PlanSaved":
            departure = conn.execute(
                "select planned_departure_at from c05_planning.plan_versions "
                "where plan_id=%s and version=3",
                (replan.plan_id,),
            ).fetchone()[0]
            assert departure == now and departure + timedelta(minutes=8) <= end
        else:
            assert db_counts(conn, replan.plan_id) == (2, 2, 1, 1)


def test_fresh_closure_after_delivery_deadline_requires_review(
    flow, replan, city, scenario, test_database_url
):
    def late_clock():
        return scenario.start_time + timedelta(days=1)

    fresh = feed_closure(
        city,
        scenario,
        late_clock,
        fresh=True,
        run_id=replan.run_id,
        correlation_id=flow.trace,
    )
    assert isinstance(fresh, Ok), fresh
    trigger = replan.trigger.model_copy(
        update={
            "source_event_id": fresh.value.queued_event_id,
            "city_snapshot_version": fresh.value.snapshot_version,
        }
    )
    change = ReplanRequest.build(replan.plan_id, trigger, replan.scope, replan.run_id)
    planning = make_replanning(test_database_url, clock=late_clock)
    result = planning.request_replan(change, flow.trace)
    assert isinstance(result, Ok) and result.value.status == "ReviewRequired", result
    assert "window" in result.value.reason.lower()
    with psycopg.connect(test_database_url) as conn:
        versions, _, events, _ = db_counts(conn, flow.plan.plan_id)
        assert (versions, events) == (2, 1)


def test_operator_can_retry_binding_after_selecting_an_undersized_van(
    flow, c08, test_database_url
):
    small = c08.client.post(
        "/api/v1/resources",
        headers=headers(c08.operator),
        json={
            **PAYLOAD,
            "external_identifier": "SMALL-VAN",
            "details": {**PAYLOAD["details"], "max_weight_kg": 1},
        },
    )
    assert small.status_code == 201, small.text
    args = (
        c08.store,
        PostgresPlanStore(test_database_url),
        flow.plan.run_scope.run_id,
        flow.plan.plan_id,
        c08.provider,
    )
    with pytest.raises(ValueError, match="rated for 1.*kg"):
        bind_c05_proposal(*args, UUID(small.json()["id"]), c08.driver)
    bind_c05_proposal(*args, c08.resource, c08.driver)
    with psycopg.connect(test_database_url) as conn:
        assert conn.execute(
            "select resource_id from c08_allocation.demo_plans "
            "where plan_id=%s and run_id=%s",
            (flow.plan.plan_id, flow.plan.run_scope.run_id),
        ).fetchall() == [(c08.resource,)]


@pytest.mark.skipif(not os.getenv("TEST_RABBITMQ_URL"), reason="requires RabbitMQ")
@pytest.mark.parametrize("driver_state", ["accepted", "pending", "revoked"])
def test_dublin_closure_revises_the_confirmed_plan_through_rabbitmq(
    flow, city, scenario, clock, c07_database, monkeypatch, c08, driver_state
):
    url = os.environ["TEST_RABBITMQ_URL"]
    exchange = f"test.hop8.{uuid4()}"
    c07_queue = exchange + ".c07"
    publisher = RabbitMQPublisher(url, exchange)
    consumer = make_consumer(
        make_detection(c07_database, clock=clock), url, exchange, c07_queue
    )
    plans = PostgresPlanOutbox(c07_database)
    plan_id, run_id = flow.plan.plan_id, flow.plan.run_scope.run_id
    bind_c05_proposal(
        c08.store,
        PostgresPlanStore(c07_database),
        run_id,
        plan_id,
        c08.provider,
        c08.resource,
        c08.driver,
    )
    allocation = allocation_consumer(c08.store, url, exchange, exchange + ".c08")
    snapshots = []
    query = LogisticsKnowledge.query_knowledge

    def capture_query(self, ctx, policy):
        assert ctx.correlation_id == flow.trace
        answer = query(self, ctx, policy)
        assert isinstance(answer, Ok), answer
        snapshots.append(
            answer.value.evidence_snapshot.model_dump(mode="json", by_alias=True)
        )
        return answer

    # Compare C09's actual return value with the evidence persisted by C10.
    monkeypatch.setattr(LogisticsKnowledge, "query_knowledge", capture_query)
    with pika.BlockingConnection(pika.URLParameters(url)) as broker:
        ch = broker.channel()
        consumer.declare()
        allocation.declare()
        inbox = ch.queue_declare(queue="", exclusive=True).method.queue
        ch.queue_bind(queue=inbox, exchange=exchange, routing_key="PlanReady")
        try:
            # The publisher drains all plans in the dedicated test database.
            with psycopg.connect(c07_database) as conn:
                conn.execute(
                    "truncate c05_planning.replan_receipts, c05_planning.outbox"
                )
            assert flow.confirm().status_code == 200
            assert drain_outbox(plans, publisher).value.published == 1
            first = receive_plan(ch, inbox)
            assert allocation.consume_available(max_messages=1) == 1
            path = f"/api/v1/vehicle-assignments?runId={run_id}"
            [initial] = c08.client.get(path, headers=headers(c08.operator)).json()
            body = {k: initial[k] for k in ("planId", "deliveryId")}
            offer = c08.client.post(
                path, headers=headers(c08.operator), json={**body, "expectedVersion": 2}
            )
            assert offer.status_code == 200, offer.text
            [pending] = c08.client.get(path, headers=headers(c08.driver)).json()

            def accept():
                return c08.client.post(
                    f"/api/v1/driver-offers/{pending['offerId']}/response?runId={run_id}",
                    headers=headers(c08.driver),
                    json={"response": "accept"},
                )

            if driver_state != "pending":
                assert accept().status_code == 200
            if driver_state == "revoked":
                with psycopg.connect(c07_database) as conn:
                    assert (
                        conn.execute(
                            "delete from c02_identity_access.provider_memberships "
                            "where provider_id=%s and principal_id=%s "
                            "and role='driver'",
                            (c08.provider, c08.driver),
                        ).rowcount
                        == 1
                    )
            closure = inject_closure(city, scenario, clock, flow)
            assert drain_outbox(city, publisher).value.published == 1
            assert consumer.consume_available(max_messages=1) == 1
            assert drain_outbox(plans, publisher).value.published == 1
            second = receive_plan(ch, inbox)
            if driver_state == "pending":
                # C05 has committed v3 but C08 still holds the v2 offer.
                assert accept().status_code == 409
            assert allocation.consume_available(max_messages=1) == 1
            publisher.publish(second)
            publisher.publish(first)
            assert allocation.consume_available(max_messages=2) == 2
            if driver_state == "pending":
                assert accept().status_code == 200
            [current] = c08.client.get(path, headers=headers(c08.operator)).json()
            assert current["assignmentRef"] == initial["assignmentRef"]
            assert current["offerId"] == pending["offerId"]
            assert current["correlationId"] == str(flow.trace)
            assert current["planVersion"] == 3
            assert current["status"] == (
                "unresolved" if driver_state == "revoked" else "accepted"
            )
            with psycopg.connect(c07_database) as conn:
                assert conn.execute(
                    "select count(*), coalesce(sum(capacity_kg) filter "
                    "(where status in ('allocated', 'offered', 'accepted')), 0) "
                    "from c08_allocation.allocations "
                    "where run_id=%s and plan_id=%s",
                    (run_id, plan_id),
                ).fetchone() == (
                    1,
                    0
                    if driver_state == "revoked"
                    else flow.plan.resource_needs[0].capacity.amount,
                )

            assert_saved_events(c07_database, flow, closure, first, second, snapshots)
            store = PostgresDisruptionStore(c07_database)
            (disruption,) = store.disruptions("simulation", run_id)
            assert disruption.trigger_event_ids == [closure.queued_event_id]
            (affected,) = disruption.affected_plans
            assert (
                affected.plan_id == plan_id and affected.replan_outcome == "PlanSaved"
            )
            assert store.processed("simulation", run_id) == {closure.queued_event_id}
            assert_c08_version_reads(flow, scenario, first, second)

            # Replay the same C06 envelope over the same broker and production handler.
            with psycopg.connect(c07_database) as conn:
                (closure_envelope,) = conn.execute(
                    "select envelope from c06_city_monitoring.outbox where event_id=%s",
                    (closure.queued_event_id,),
                ).fetchone()
            publisher.publish(EventEnvelope.model_validate(closure_envelope))
            assert consumer.consume_available(max_messages=1) == 1
            assert drain_outbox(plans, publisher).value.published == 0
            assert receive_plan(ch, inbox) == second
            assert receive_plan(ch, inbox) == first
            assert ch.basic_get(inbox)[0] is None
        finally:
            consumer.close()
            allocation.close()
            publisher.close()
            ch.queue_delete(queue=c07_queue)
            ch.queue_delete(queue=exchange + ".c08")
            ch.exchange_delete(exchange=exchange)


def assert_saved_events(conninfo, flow, closure, first, second, snapshots):
    plan_id, run_id = flow.plan.plan_id, flow.plan.run_scope.run_id
    (delivery_id,) = flow.plan.request_ids
    with psycopg.connect(conninfo) as conn:
        saved = conn.execute(
            """select event_id, envelope, published_at from c05_planning.outbox
                where plan_id=%s order by version""",
            (plan_id,),
        ).fetchall()
        (record,) = conn.execute(
            "select record from c10_decision_records.decisions where decision_id=%s",
            (second.payload.decision_id,),
        ).fetchone()
    assert len(saved) == 2
    for expected_version, event, row in zip(
        (2, 3), (first, second), saved, strict=True
    ):
        assert event.payload.plan_id == plan_id
        assert event.payload.plan_version == expected_version
        assert event.payload.requirements == flow.plan.resource_needs
        assert (event.run_id, event.correlation_id) == (run_id, flow.trace)
        assert event.event_id == row[0] and row[2] is not None
        assert PlanReadyEvent.model_validate_json(json.dumps(row[1])) == event
    assert first.event_id != second.event_id
    assert second.causation_id == closure.queued_event_id
    assert second.payload.decision_id != first.payload.decision_id
    assert record["subject"] == {
        "type": "Plan",
        "id": str(plan_id),
        "version": 3,
    }
    assert record["decisionType"] == "Replan" and record["attemptOutcome"] == "Selected"
    assert record["relatedDecisionId"] == str(first.payload.decision_id)
    assert record["inputVersions"]["citySnapshotVersion"] == closure.snapshot_version
    assert record["inputVersions"]["deliveries"] == [
        {"id": str(delivery_id), "version": 2}
    ]
    assert snapshots and all(s["items"] for s in snapshots)
    assert record["evidenceSnapshots"] == snapshots


def assert_c08_version_reads(flow, scenario, first, second):
    plan_id = flow.plan.plan_id
    (delivery_id,) = flow.plan.request_ids
    with flow.client.app.state.engine.begin() as conn:
        scope = flow.plan.run_scope
        for event, name in [
            (first, "Eden Quay"),
            (second, "Abbey Street Lower"),
        ]:
            segment = next(s for s in scenario.segments if s.name == name)
            read = get_delivery_route(
                conn, scope, plan_id, event.payload.plan_version, delivery_id
            )
            assert isinstance(read, Ok), read
            assert read.value.plan.decision_id == event.payload.decision_id
            assert read.value.route.segment_keys == [segment.segment_key]
            assert read.value.planned_departure_at.utcoffset() is not None
        old = check_current_version(conn, scope, plan_id, 2)
        assert isinstance(old, Failure) and old.kind == "Conflict"
        assert check_current_version(conn, scope, plan_id, 3) == Ok(None)


def receive_plan(ch, queue):
    method, props, raw = ch.basic_get(queue)
    assert method is not None and method.routing_key == "PlanReady"
    event = PlanReadyEvent.model_validate_json(raw)
    assert props.message_id == str(event.event_id)
    ch.basic_ack(method.delivery_tag)
    return event
