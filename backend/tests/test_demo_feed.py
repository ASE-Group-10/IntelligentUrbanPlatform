"""Demo feed script: inject the scenario's road closure into C06 (#42)."""

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.components.c06_city_monitoring.store import (
    InMemoryCityStateStore,
    StateScope,
    StoreUnavailableError,
)
from app.demo.feed import SCENARIO_PATH, feed_closure, load_scenario
from app.shared.result import Failure, Ok

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def scenario():
    return load_scenario(SCENARIO_PATH)


@pytest.fixture
def store(new_store):
    return new_store()


def run_scope(scenario) -> StateScope:
    return StateScope(scope="simulation", run_id=scenario.run_id)


def test_shared_scenario_uses_the_agreed_demo_values(scenario, example):
    observation = example("observation-closure.json")
    event = example("city-state-updated-event.json")

    assert scenario.closure.segment_key == "seg:4471023"
    assert str(scenario.run_id) == event["runId"]
    assert str(scenario.correlation_id) == event["correlationId"]
    assert str(scenario.closure.observation_id) == observation["observationId"]


def test_first_feed_closes_the_segment_and_queues_an_event(store, scenario):
    result = feed_closure(store, scenario, clock=lambda: NOW)

    assert isinstance(result, Ok)
    report = result.value
    assert report.disposition == "merged"
    assert report.already_ingested is False
    assert report.snapshot_version == 1
    assert report.correlation_id == scenario.correlation_id
    assert report.segment_status == "closed"
    state = store.state(run_scope(scenario))
    assert [e.event_id for e in state.outbox] == [report.queued_event_id]


def test_feeding_again_changes_nothing(store, scenario):
    first = feed_closure(store, scenario, clock=lambda: NOW).value

    again = feed_closure(store, scenario, clock=lambda: NOW).value

    assert again.already_ingested is True
    assert again.queued_event_id is None
    assert again.snapshot_version == first.snapshot_version
    assert len(store.state(run_scope(scenario)).outbox) == 1


def test_fresh_feed_injects_a_new_closure_with_new_ids(store, scenario):
    first = feed_closure(store, scenario, clock=lambda: NOW).value

    fresh = feed_closure(store, scenario, clock=lambda: NOW, fresh=True).value

    assert fresh.already_ingested is False
    assert fresh.snapshot_version == first.snapshot_version + 1
    assert fresh.observation_id != scenario.closure.observation_id
    assert fresh.correlation_id != scenario.correlation_id
    assert isinstance(fresh.correlation_id, UUID)


def test_unavailable_store_is_reported(scenario):
    class StoreDown(InMemoryCityStateStore):
        @contextmanager
        def transaction(self, state_scope):
            raise StoreUnavailableError("database unreachable")
            yield  # pragma: no cover

    result = feed_closure(StoreDown(), scenario, clock=lambda: NOW)

    assert isinstance(result, Failure)
    assert result.kind == "Unavailable"
