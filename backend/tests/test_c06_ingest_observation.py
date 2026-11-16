"""UC05 ingestObservation: closure case, idempotency, AF4 and live/simulation scope."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.components.c06_city_monitoring.contracts import CityStateUpdated, Observation
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.store import (
    InMemoryCityStateStore,
    InMemoryTransaction,
    StateScope,
    StoreUnavailableError,
)
from app.shared.contracts import EventEnvelope
from app.shared.result import Failure, Ok

EVENT_ID = UUID("5b1e0c1a-8f0e-4a7e-9d6c-1f2a3b4c5d6e")
CORRELATION_ID = UUID("a7d9e2f4-3c1b-4b8a-9e6f-0d1c2b3a4f5e")
RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
NOW = datetime(2026, 11, 20, 8, 14, 31, tzinfo=UTC)
SEGMENT = "seg:4471023"

SIMULATION = StateScope(scope="simulation", run_id=RUN_ID)
LIVE = StateScope(scope="live")


def make_service(store: InMemoryCityStateStore) -> CityMonitoring:
    return CityMonitoring(store=store, clock=lambda: NOW, new_id=lambda: EVENT_ID)


def context(state_scope: StateScope = SIMULATION) -> SourceContext:
    return SourceContext(state_scope=state_scope, correlation_id=CORRELATION_ID)


@pytest.fixture
def closure(example) -> Observation:
    return Observation.model_validate(example("observation.closure.json"))


@pytest.fixture
def store() -> InMemoryCityStateStore:
    store = InMemoryCityStateStore()
    store.seed_version(SIMULATION, 41)
    return store


class TestClosureIsMerged:
    def test_receipt_is_merged_at_next_version(self, store, closure):
        result = make_service(store).ingest_observation(context(), closure)

        assert isinstance(result, Ok)
        assert result.value.disposition == "merged"
        assert result.value.snapshot_version == 42
        assert result.value.observation_id == closure.observation_id

    def test_segment_is_closed_in_new_city_state_version(self, store, closure):
        make_service(store).ingest_observation(context(), closure)

        state = store.state(SIMULATION)
        assert state.version == 42
        segment = state.segments[SEGMENT]
        assert segment.status == "closed"
        assert segment.cause == "roadworks"
        assert segment.quality == "good"
        assert segment.sources == ["sim-traffic"]

    def test_observation_is_kept_in_history(self, store, closure):
        make_service(store).ingest_observation(context(), closure)

        history = store.state(SIMULATION).history
        assert [(o.observation_id, r.disposition) for o, r in history] == [
            (closure.observation_id, "merged")
        ]

    def test_city_state_updated_matches_contract_example(self, store, closure, example):
        make_service(store).ingest_observation(context(), closure)

        (queued,) = store.state(SIMULATION).outbox
        expected = EventEnvelope.model_validate(
            example("event_envelope.city_state_updated.json")
        )
        assert queued.model_dump(exclude={"payload"}) == expected.model_dump(
            exclude={"payload"}
        )
        assert CityStateUpdated.model_validate(
            queued.payload
        ) == CityStateUpdated.model_validate(expected.payload)

    def test_first_observation_moves_city_state_from_zero_to_one(self, closure):
        store = InMemoryCityStateStore()

        result = make_service(store).ingest_observation(context(), closure)

        assert result.value.snapshot_version == 1
        payload = CityStateUpdated.model_validate(
            store.state(SIMULATION).outbox[0].payload
        )
        assert (payload.previous_version, payload.snapshot_version) == (0, 1)


def test_same_observation_twice_returns_first_receipt_and_changes_nothing(
    store, closure
):
    service = make_service(store)
    first = service.ingest_observation(context(), closure)

    second = service.ingest_observation(context(), closure)

    assert second == first
    state = store.state(SIMULATION)
    assert state.version == 42
    assert len(state.outbox) == 1
    assert len(state.history) == 1


def test_older_observation_is_history_only(store, closure):
    service = make_service(store)
    service.ingest_observation(context(), closure)
    older = closure.model_copy(
        update={
            "observation_id": UUID("1f2e3d4c-5b6a-4978-8695-a4b3c2d1e0f9"),
            "values": {"status": "open"},
            "observed_at": datetime(2026, 11, 20, 8, 0, tzinfo=UTC),
        }
    )

    result = service.ingest_observation(context(), older)

    assert result.value.disposition == "history_only"
    assert result.value.snapshot_version is None
    state = store.state(SIMULATION)
    assert state.version == 42
    assert state.segments[SEGMENT].status == "closed"
    assert len(state.outbox) == 1
    assert [r.disposition for _, r in state.history] == ["merged", "history_only"]


def test_simulation_observation_does_not_touch_live_state(store, closure):
    make_service(store).ingest_observation(context(SIMULATION), closure)

    live = store.state(LIVE)
    assert live.version == 0
    assert live.segments == {}
    assert live.outbox == []


def test_live_observation_emits_live_event_without_run_id(closure):
    store = InMemoryCityStateStore()

    make_service(store).ingest_observation(context(LIVE), closure)

    (queued,) = store.state(LIVE).outbox
    assert (queued.scope, queued.run_id) == ("live", None)


def test_failed_commit_stores_nothing(closure):
    class OutboxDown(InMemoryTransaction):
        def enqueue(self, envelope: EventEnvelope) -> None:
            raise StoreUnavailableError("outbox write failed")

    store = InMemoryCityStateStore(transaction_type=OutboxDown)
    store.seed_version(SIMULATION, 41)

    result = make_service(store).ingest_observation(context(), closure)

    assert isinstance(result, Failure)
    assert result.kind == "Unavailable"
    state = store.state(SIMULATION)
    assert (state.version, state.segments, state.history, state.outbox) == (
        41,
        {},
        [],
        [],
    )


@pytest.mark.parametrize(
    "update",
    [
        {"values": {"status": "blocked"}},
        {"values": {}},
        {"target": {"zoneKey": "zone:D02"}},
        {"target": {"lat": 53.3438, "lon": -6.2546}},
    ],
    ids=["unknown-status", "no-status", "zone-target", "point-target"],
)
def test_invalid_or_unsupported_observation_is_rejected(
    update, store, closure, example
):
    data = example("observation.closure.json") | update
    observation = Observation.model_validate(data)

    result = make_service(store).ingest_observation(context(), observation)

    assert isinstance(result, Failure)
    assert result.kind == "Invalid"
    state = store.state(SIMULATION)
    assert (state.version, state.history, state.outbox) == (41, [], [])


class TestStateScope:
    def test_simulation_requires_run_id(self):
        with pytest.raises(ValueError):
            StateScope(scope="simulation")

    def test_live_must_not_have_run_id(self):
        with pytest.raises(ValueError):
            StateScope(scope="live", run_id=RUN_ID)
