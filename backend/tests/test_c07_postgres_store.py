"""Postgres-only behaviour of the C07 store: restarts, concurrency, failures.

Everything else is covered by running ``test_c07_evaluate_change.py`` against
both stores (its ``store`` fixture).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ReplanOutcome,
)
from app.components.c07_disruption_detection.detection import DisruptionDetection
from app.components.c07_disruption_detection.postgres_store import (
    PostgresDisruptionStore,
)
from app.shared.contracts import EventEnvelope
from app.shared.result import Failure, Ok
from tests.c07_planning_double import FakePlanning

RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
NOW = datetime(2026, 11, 20, 8, 14, 32, tzinfo=UTC)
SEGMENT = "seg:4471023"


@pytest.fixture
def planning(example) -> FakePlanning:
    work = AffectedWork.model_validate(example("find-affected-work-response.json"))
    return FakePlanning(
        work_by_segment={SEGMENT: work},
        outcome=ReplanOutcome(
            status="PlanSaved", new_plan_version=4, decision_id=uuid4()
        ),
    )


@pytest.fixture
def closure(example) -> EventEnvelope:
    return EventEnvelope.model_validate(example("city-state-updated-event.json"))


def detection(planning, url: str) -> DisruptionDetection:
    return DisruptionDetection(
        planning=planning, store=PostgresDisruptionStore(url), clock=lambda: NOW
    )


def test_disruption_survives_a_worker_restart(planning, closure, c07_database):
    assert isinstance(detection(planning, c07_database).evaluate_change(closure), Ok)

    # a new process: new store, same database
    restarted = detection(planning, c07_database)
    again = restarted.evaluate_change(closure)

    assert isinstance(again, Ok) and again.value.kind == "duplicate"
    (saved,) = PostgresDisruptionStore(c07_database).disruptions("simulation", RUN_ID)
    assert saved.segment_keys == [SEGMENT]
    assert saved.affected_plans[0].replan_outcome == "PlanSaved"
    assert len(planning.replan_calls) == 1


def test_concurrent_deliveries_of_one_event_record_one_disruption(
    planning, closure, c07_database
):
    service = detection(planning, c07_database)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.evaluate_change(closure), range(8)))

    assert all(isinstance(r, Ok) for r in results)
    assert [r.value.kind for r in results].count("created") == 1
    store = PostgresDisruptionStore(c07_database)
    assert len(store.disruptions("simulation", RUN_ID)) == 1
    assert store.processed("simulation", RUN_ID) == {closure.event_id}
    # a redelivery may resend a replan C05 hasn't answered yet, always with one key
    assert len({request.key for request, _ in planning.replan_calls}) == 1


def test_overlapping_redeliveries_preserve_both_replans(example, closure, c07_database):
    work = AffectedWork.model_validate(example("find-affected-work-response.json"))
    first = work.items[0]
    second = first.model_copy(update={"plan_id": uuid4()})
    work = AffectedWork(items=[first, second])

    class SecondWorkerPlanning(FakePlanning):
        def request_replan(self, request, correlation_id):
            self.replan_unavailable = request.plan_id != second.plan_id
            return super().request_replan(request, correlation_id)

    other = detection(
        SecondWorkerPlanning(work_by_segment={SEGMENT: work}), c07_database
    )

    class FirstWorkerPlanning(FakePlanning):
        def request_replan(self, request, correlation_id):
            if request.plan_id == first.plan_id:
                # The duplicate finishes while this worker is waiting for C05.
                duplicate = other.evaluate_change(closure)
                assert isinstance(duplicate, Ok) and duplicate.value.kind == "duplicate"
                assert [r.plan_id for r in duplicate.value.replan_requests] == [
                    second.plan_id
                ]
            self.replan_unavailable = request.plan_id != first.plan_id
            return super().request_replan(request, correlation_id)

    worker = detection(
        FirstWorkerPlanning(work_by_segment={SEGMENT: work}), c07_database
    )
    original = worker.evaluate_change(closure)
    assert isinstance(original, Ok)
    assert [r.plan_id for r in original.value.replan_requests] == [first.plan_id]
    (saved,) = PostgresDisruptionStore(c07_database).disruptions("simulation", RUN_ID)
    assert {p.plan_id: p.replan_outcome for p in saved.affected_plans} == {
        first.plan_id: "NoChange",
        second.plan_id: "NoChange",
    }
    again = worker.evaluate_change(closure)
    assert isinstance(again, Ok) and again.value.replan_requests == []


def test_failed_transaction_leaves_nothing_behind(closure, c07_database):
    store = PostgresDisruptionStore(c07_database)

    with pytest.raises(RuntimeError), store.transaction("simulation", RUN_ID) as tx:
        tx.mark_processed(closure.event_id)
        raise RuntimeError("crash before commit")

    assert store.processed("simulation", RUN_ID) == set()


def test_live_and_each_run_are_kept_apart(planning, closure, c07_database):
    other_run = uuid4()
    service = detection(planning, c07_database)
    service.evaluate_change(closure)
    service.evaluate_change(closure.model_copy(update={"run_id": other_run}))

    store = PostgresDisruptionStore(c07_database)
    assert len(store.disruptions("simulation", RUN_ID)) == 1
    assert len(store.disruptions("simulation", other_run)) == 1
    assert store.disruptions("live", None) == []


def test_unreachable_database_is_unavailable(planning, closure):
    url = "postgres://ulop:ulop@127.0.0.1:1/ulop_test?connect_timeout=2"

    result = detection(planning, url).evaluate_change(closure)

    assert isinstance(result, Failure) and result.kind == "Unavailable"
    assert planning.find_calls == []
