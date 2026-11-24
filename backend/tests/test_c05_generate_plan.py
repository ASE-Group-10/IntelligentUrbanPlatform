"""Hop 4 from C04's saved delivery shape through real PostgreSQL, C09 and C10."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from app.components.c04_delivery_management.planning import seed_demo_delivery
from app.components.c05_planning.facts import find_affected_work
from app.components.c05_planning.models import DeliveryWindow, PlanScope, SavedDelivery
from app.components.c05_planning.service import PlanningService
from app.components.c05_planning.store import PostgresPlanStore
from app.components.c07_disruption_detection.contracts import ChangeScope
from app.components.c09_logistics_knowledge.contracts import RuleCoverage
from app.components.c09_logistics_knowledge.demo_corpus import DEMO_ZONES, load
from app.components.c09_logistics_knowledge.postgres_store import PostgresKnowledgeStore
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import DecisionRecords
from app.demo.c05 import fresh_delivery, generate_saved_demo
from app.demo.feed import load_scenario
from app.shared.auth import AuthContext
from app.shared.result import Failure, Ok


@pytest.fixture
def scenario():
    return load_scenario()


@pytest.fixture
def request_args(scenario, example, test_database_url):
    saved = example("delivery-submitted.json")
    saved["deliveryId"] = str(uuid4())
    with psycopg.connect(test_database_url) as conn:
        seed_demo_delivery(conn, scenario.run_id, saved)
    delivery = SavedDelivery.model_validate(saved)
    scope = PlanScope(scope="simulation", run_id=scenario.run_id)
    return delivery, scope, scenario.correlation_id, f"hop4-{uuid4()}"


@pytest.fixture
def knowledge(test_database_url, scenario):
    with psycopg.connect(test_database_url) as conn:
        conn.execute(
            "truncate c09_logistics_knowledge.publish_receipts, "
            "c09_logistics_knowledge.knowledge_warnings, "
            "c09_logistics_knowledge.corpus_versions, "
            "c09_logistics_knowledge.knowledge_chunks, "
            "c09_logistics_knowledge.knowledge_documents"
        )
    knowledge = LogisticsKnowledge(
        PostgresKnowledgeStore(test_database_url, DEMO_ZONES),
        clock=lambda: scenario.start_time,
    )
    assert all(isinstance(result, Ok) for result in load(knowledge))
    return knowledge


@pytest.fixture
def store(test_database_url):
    return PostgresPlanStore(test_database_url)


@pytest.fixture
def decisions(scenario):
    return DecisionRecords(PostgresDecisionStore(), clock=lambda: scenario.start_time)


@pytest.fixture
def planner(store, knowledge, decisions, scenario):
    return PlanningService(
        store, knowledge, decisions, scenario, lambda: scenario.start_time
    )


def row_counts(conn):
    return conn.execute(
        "select (select count(*) from c05_planning.plan_versions), "
        "       (select count(*) from c10_decision_records.decisions), "
        "       (select count(*) from c05_planning.generation_receipts), "
        "       (select count(*) from c05_planning.outbox)"
    ).fetchone()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "version", "1"),
        (None, "version", True),
        ("cargo", "weightKg", "4.5"),
        ("cargo", "weightKg", True),
        ("timeWindow", "start", 1795165200),
        ("timeWindow", "end", "1795176000"),
        ("timeWindow", "start", "2026-11-20T09:00:00+0000"),
    ],
)
def test_invalid_saved_delivery_facts_cannot_create_a_proposal(
    planner, example, test_database_url, section, field, value
):
    saved = example("delivery-submitted.json")
    fields = saved if section is None else saved[section]
    fields[field] = value
    scope = PlanScope(scope="simulation", run_id=uuid4())
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        before = row_counts(observer)
        with pytest.raises(ValidationError):
            generate_saved_demo(planner, test_database_url, scope, saved, uuid4())
        assert row_counts(observer) == before
        assert observer.execute(
            "select count(*) from c04_delivery_management.deliveries where run_id=%s",
            (scope.run_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "start",
    [
        "2026-11-20T09:00:00.123+00:00",
        "2026-11-20t09:00:00.123z",
        datetime(2026, 11, 20, 9, 0, 0, 123000, UTC),
    ],
)
def test_delivery_window_accepts_rfc3339_utc_and_typed_datetimes(start):
    window = DeliveryWindow(start=start, end="2026-11-20T12:00:00Z")
    assert window.start == datetime(2026, 11, 20, 9, 0, 0, 123000, UTC)


def test_fresh_delivery_creates_distinct_utc_examples_across_midnight(example):
    original = example("delivery-submitted.json")
    now = datetime.fromisoformat("2026-11-28T00:50:00+01:00")
    fresh = fresh_delivery(original, now)
    second = fresh_delivery(original, now)
    assert len({original["deliveryId"], fresh["deliveryId"], second["deliveryId"]}) == 3
    assert UUID(fresh["deliveryId"]).version == 4
    assert fresh["createdAt"] == fresh["updatedAt"] == fresh["progress"]["updatedAt"]
    assert fresh["createdAt"] == "2026-11-27T23:50:00Z"
    assert fresh["timeWindow"] == {
        "start": "2026-11-28T00:20:00Z",
        "end": "2026-11-28T03:20:00Z",
    }
    assert fresh["cargo"] == original["cargo"]
    assert original == example("delivery-submitted.json")


@pytest.mark.parametrize(
    "fresh", [False, True], ids=["original-window", "fresh-window"]
)
def test_saved_demo_preserves_original_trace_and_cannot_replan_a_linked_delivery(
    planner, store, example, test_database_url, fresh
):
    saved = example("delivery-submitted.json")
    demo_day = datetime(2026, 11, 27, 9, tzinfo=UTC)
    if fresh:
        saved = fresh_delivery(saved, demo_day)
    scope = PlanScope(scope="simulation", run_id=uuid4())
    first_trace = uuid4()

    def generate(trace, key=None):
        return generate_saved_demo(planner, test_database_url, scope, saved, trace, key)

    original = generate(first_trace)
    assert isinstance(original, Ok)
    plan, trace = original.value
    assert trace == first_trace
    assert generate(uuid4()) == original
    # A caller can choose a different key and trace while the delivery is pending.
    override_trace = uuid4()
    alternate = generate(override_trace, "prior-to-confirmation")
    assert isinstance(alternate, Ok) and alternate.value[1] == override_trace
    assert alternate.value[0].plan_id != plan.plan_id

    provider = uuid4()
    operator = AuthContext(
        principal_id=uuid4(),
        principal_type="user",
        acting_provider_id=provider,
        roles=("operator",),
        provider_ids=(provider,),
        platform_roles=(),
        correlation_id=first_trace,
    )
    assert isinstance(
        planner.confirm_plan(operator, scope, plan.plan_id, 1, "confirm"), Ok
    )
    affected = find_affected_work(
        store,
        ChangeScope(
            scope="simulation",
            run_id=scope.run_id,
            segment_keys=plan.routes[0].segment_keys,
        ),
        start=demo_day + timedelta(minutes=15),
    )
    assert isinstance(affected, Ok)
    assert [item.plan_id for item in affected.value.items] == (
        [plan.plan_id] if fresh else []
    )
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        before = row_counts(observer)
        assert generate(uuid4()) == original
        rejected = generate(uuid4(), "new-after-confirmation")
        assert isinstance(rejected, Failure) and rejected.kind == "Conflict"
        assert row_counts(observer) == before


def test_proposal_records_exact_c09_evidence_and_original_trace(
    planner, knowledge, request_args, scenario, test_database_url, monkeypatch, caplog
):
    delivery, scope, cid, _ = request_args
    snapshots = []
    query = knowledge.query_knowledge

    def observe_snapshot(ctx, policy_query):
        result = query(ctx, policy_query)
        assert isinstance(result, Ok)
        snapshots.append(
            result.value.evidence_snapshot.model_dump(mode="json", by_alias=True)
        )
        return result

    monkeypatch.setattr(knowledge, "query_knowledge", observe_snapshot)
    caplog.set_level(logging.INFO)
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        plans, records, receipts, outbox = row_counts(observer)
        result = planner.generate_plan(*request_args)
        assert isinstance(result, Ok), result
        plan = result.value
        assert row_counts(observer) == (plans + 1, records + 1, receipts + 1, outbox)
        # Publishing a new corpus version must not replace the decision-time evidence.
        amendment = load(knowledge, include_conflict=True)[-1]
        assert isinstance(amendment, Ok) and not amendment.value.duplicate
        assert amendment.value.corpus_version > snapshots[0]["corpusVersion"]
        [(record,)] = observer.execute(
            "select record from c10_decision_records.decisions where decision_id = %s",
            (plan.decision_id,),
        ).fetchall()
        [(departure,)] = observer.execute(
            "select planned_departure_at from c05_planning.plan_versions "
            "where plan_id = %s and version = %s",
            (plan.plan_id, plan.version),
        ).fetchall()

    assert planner.get_plan(scope, plan.plan_id) == result
    assert plan.status == "awaiting_confirmation"
    assert plan.routes[0].segment_keys == [scenario.closure.segment_key]
    assert plan.request_ids == [delivery.delivery_id]
    assert plan.resource_needs[0].capacity.amount == delivery.cargo.weight_kg
    assert plan.input_versions[f"delivery:{delivery.delivery_id}"] == delivery.version
    assert departure == delivery.time_window.start
    assert departure == datetime.fromisoformat(
        record["selectedOption"]["details"]["plannedDepartureAt"]
    )
    assert record["subject"] == {
        "type": "Plan",
        "id": str(plan.plan_id),
        "version": plan.version,
    }
    assert record["attemptOutcome"] == "Selected"
    assert record["runScope"] == {"mode": "Simulation", "runId": str(scope.run_id)}
    assert snapshots and all(s["items"] for s in snapshots)
    assert record["evidenceSnapshots"] == snapshots
    traces = {
        (line.component, line.scope, str(getattr(line, "runId", "")))
        for line in caplog.records
        if str(getattr(line, "correlationId", "")) == str(cid)
    }
    assert ("C05", "simulation", str(scope.run_id)) in traces
    assert ("C09", "live", "") in traces
    assert ("C10", "simulation", str(scope.run_id)) in traces

    wrong_run = PlanScope(scope="simulation", run_id=uuid4())
    missing = planner.get_plan(wrong_run, plan.plan_id)
    assert isinstance(missing, Failure) and missing.kind == "NotFound"


@pytest.mark.parametrize("start", ["2026-11-20T09:00:00Z", "9999-12-31T23:58:00Z"])
def test_route_must_fit_the_saved_delivery_window(
    planner, example, test_database_url, start
):
    departure = datetime.fromisoformat(start)
    window = DeliveryWindow(start=departure, end=departure + timedelta(minutes=1))
    saved = example("delivery-submitted.json")
    saved["timeWindow"] = window.model_dump(mode="json")
    scope = PlanScope(scope="simulation", run_id=uuid4())
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        seed_demo_delivery(observer, scope.run_id, saved)
        before = row_counts(observer)
        result = planner.generate_plan(
            SavedDelivery.model_validate(saved), scope, uuid4(), "short-window"
        )
        assert isinstance(result, Failure) and result.kind == "Infeasible"
        assert row_counts(observer) == before


def test_database_failure_after_both_inserts_rolls_back_and_allows_retry(
    planner, store, request_args, test_database_url, monkeypatch
):
    save = store.save
    reached_write = False
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        before = row_counts(observer)

        def break_transaction(conn, *args, **kwargs):
            nonlocal reached_write
            save(conn, *args, **kwargs)
            # Both writes exist on the caller's connection. Neither is visible
            # on a fresh snapshot outside the transaction.
            assert row_counts(conn)[:2] == (before[0] + 1, before[1] + 1)
            assert row_counts(observer) == before
            reached_write = True
            conn.execute("select 1 / 0")

        with monkeypatch.context() as patch:
            patch.setattr(store, "save", break_transaction)
            result = planner.generate_plan(*request_args)
        assert reached_write
        assert isinstance(result, Failure) and result.kind == "Unavailable"
        assert row_counts(observer) == before
        assert isinstance(planner.generate_plan(*request_args), Ok)
        assert row_counts(observer) == (
            before[0] + 1,
            before[1] + 1,
            before[2] + 1,
            before[3],
        )


def test_incomplete_policy_coverage_cannot_generate_a_selected_plan(
    planner, knowledge, request_args, test_database_url, monkeypatch
):
    query = knowledge.query_knowledge

    def incomplete(ctx, policy_query):
        result = query(ctx, policy_query)
        assert isinstance(result, Ok)
        update = {
            "rule_coverage": RuleCoverage.INCOMPLETE,
            "limitations": ["missing rules"],
        }
        snapshot = result.value.evidence_snapshot.model_copy(update=update)
        return Ok(
            result.value.model_copy(update={**update, "evidence_snapshot": snapshot})
        )

    monkeypatch.setattr(knowledge, "query_knowledge", incomplete)
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        before = row_counts(observer)
        result = planner.generate_plan(*request_args)
        assert isinstance(result, Failure) and "DATA_UNAVAILABLE" in result.message
        assert row_counts(observer) == before


def test_c10_failure_result_aborts_its_own_uncommitted_write(
    planner, decisions, request_args, test_database_url, monkeypatch
):
    record_decision = decisions.record_decision

    def refuse_after_write(conn, ctx, record):
        assert isinstance(record_decision(conn, ctx, record), Ok)
        return Failure("Unavailable", "injected C10 failure after insert")

    monkeypatch.setattr(decisions, "record_decision", refuse_after_write)
    with psycopg.connect(test_database_url, autocommit=True) as observer:
        before = row_counts(observer)
        result = planner.generate_plan(*request_args)
        assert isinstance(result, Failure) and result.kind == "Unavailable"
        assert row_counts(observer) == before


def test_concurrent_retries_save_one_result_and_changed_input_conflicts(
    planner, request_args, test_database_url
):
    delivery, scope, cid, key = request_args
    start = Barrier(2)

    def generate():
        start.wait(timeout=5)
        return planner.generate_plan(*request_args)

    with psycopg.connect(test_database_url, autocommit=True) as observer:
        plans, records, receipts, outbox = row_counts(observer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(generate)
            second = pool.submit(generate)
            result = first.result(timeout=15)
            assert isinstance(result, Ok), result
            assert second.result(timeout=15) == result

        changed = delivery.model_copy(update={"version": delivery.version + 1})
        conflict = planner.generate_plan(changed, scope, cid, key)
        assert isinstance(conflict, Failure) and conflict.kind == "Conflict"
        assert row_counts(observer) == (plans + 1, records + 1, receipts + 1, outbox)
