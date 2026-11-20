"""UC05 getCitySnapshot: versioned City State with freshness, history and scope."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.components.c06_city_monitoring.contracts import (
    CitySnapshot,
    CitySnapshotQuery,
    Observation,
)
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.store import (
    InMemoryCityStateStore,
    InMemoryReader,
    StateScope,
    StoreUnavailableError,
)
from app.shared.result import Failure, Ok

CORRELATION_ID = UUID("a7d9e2f4-3c1b-4b8a-9e6f-0d1c2b3a4f5e")
RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
OBSERVED = datetime(2026, 11, 20, 8, 14, 30, tzinfo=UTC)
SEGMENT = "seg:4471023"
OTHER_SEGMENT = "seg:5550001"

SIMULATION = StateScope(scope="simulation", run_id=RUN_ID)
LIVE = StateScope(scope="live")


class Clock:
    """Controllable clock so freshness can be tested at exact boundaries."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 20, 8, 14, 35, tzinfo=UTC))


@pytest.fixture
def store(new_store):
    store = new_store()
    store.seed_version(SIMULATION, 41)
    return store


@pytest.fixture
def service(store, clock) -> CityMonitoring:
    return CityMonitoring(store=store, clock=clock)


@pytest.fixture
def closure(example) -> Observation:
    return Observation.model_validate(example("observation-closure.json"))


def ingest(service: CityMonitoring, observation: Observation, scope=SIMULATION) -> None:
    context = SourceContext(state_scope=scope, correlation_id=CORRELATION_ID)
    assert isinstance(service.ingest_observation(context, observation), Ok)


def observation_on(segment: str, status: str, observed_at: datetime, oid: str):
    return Observation.model_validate(
        {
            "observationId": oid,
            "sourceId": "sim-traffic",
            "kind": "traffic",
            "target": {"segmentKey": segment},
            "values": {"status": status},
            "observedAt": observed_at.isoformat().replace("+00:00", "Z"),
        }
    )


def query(*segments: str, max_age: int | None = None, at_version: int | None = None):
    data: dict = {"scope": {"segmentKeys": list(segments)}}
    if max_age is not None:
        data["freshnessPolicy"] = {"maxAgeSeconds": max_age}
    if at_version is not None:
        data["atVersion"] = at_version
    return CitySnapshotQuery.model_validate(data)


class TestCurrentSnapshot:
    def test_closure_snapshot_matches_contract_example(self, service, closure, example):
        ingest(service, closure)

        result = service.get_city_snapshot(
            SIMULATION,
            CitySnapshotQuery.model_validate(example("city-snapshot-query.json")),
        )

        assert isinstance(result, Ok)
        assert result.value == CitySnapshot.model_validate(
            example("city-snapshot.json")
        )

    def test_only_requested_segments_are_returned(self, service, closure):
        ingest(service, closure)
        ingest(
            service,
            observation_on(
                OTHER_SEGMENT, "slow", OBSERVED, "11111111-1111-4111-8111-111111111111"
            ),
        )

        result = service.get_city_snapshot(SIMULATION, query(OTHER_SEGMENT))

        assert [s.segment_key for s in result.value.segments] == [OTHER_SEGMENT]
        assert result.value.snapshot_version == 43

    def test_segments_without_a_condition_are_omitted(self, service, closure):
        ingest(service, closure)

        result = service.get_city_snapshot(SIMULATION, query(SEGMENT, "seg:999"))

        assert [s.segment_key for s in result.value.segments] == [SEGMENT]
        assert result.value.overall_quality == "good"


class TestFreshness:
    def test_value_at_the_default_limit_is_still_good(self, service, closure, clock):
        ingest(service, closure)
        clock.now = OBSERVED + timedelta(seconds=900)

        snapshot = service.get_city_snapshot(SIMULATION, query(SEGMENT)).value

        assert snapshot.segments[0].quality == "good"
        assert snapshot.stale_sources == []

    def test_older_value_is_degraded_and_its_source_stale(
        self, service, closure, clock
    ):
        ingest(service, closure)
        clock.now = OBSERVED + timedelta(seconds=901)

        snapshot = service.get_city_snapshot(SIMULATION, query(SEGMENT)).value

        assert snapshot.segments[0].quality == "degraded"
        assert snapshot.overall_quality == "degraded"
        assert snapshot.stale_sources == ["sim-traffic"]
        assert snapshot.segments[0].status == "closed"

    def test_caller_can_tighten_the_freshness_limit(self, service, closure, clock):
        ingest(service, closure)
        clock.now = OBSERVED + timedelta(seconds=61)

        snapshot = service.get_city_snapshot(
            SIMULATION, query(SEGMENT, max_age=60)
        ).value

        assert snapshot.overall_quality == "degraded"

    def test_overall_quality_is_the_worst_returned_value(self, service, closure, clock):
        ingest(service, closure)
        fresh = OBSERVED + timedelta(seconds=1000)
        ingest(
            service,
            observation_on(
                OTHER_SEGMENT, "slow", fresh, "22222222-2222-4222-8222-222222222222"
            ),
        )
        clock.now = fresh

        snapshot = service.get_city_snapshot(
            SIMULATION, query(SEGMENT, OTHER_SEGMENT)
        ).value

        assert {s.segment_key: s.quality for s in snapshot.segments} == {
            SEGMENT: "degraded",
            OTHER_SEGMENT: "good",
        }
        assert snapshot.overall_quality == "degraded"


class TestPastVersions:
    @pytest.fixture
    def reopened(self, service, closure):
        """Closure at version 42, then the road reopens at version 43."""
        ingest(service, closure)
        later = OBSERVED + timedelta(minutes=30)
        ingest(
            service,
            observation_on(
                SEGMENT, "open", later, "33333333-3333-4333-8333-333333333333"
            ),
        )
        return service

    def test_current_snapshot_shows_the_latest_condition(self, reopened):
        snapshot = reopened.get_city_snapshot(SIMULATION, query(SEGMENT)).value

        assert snapshot.snapshot_version == 43
        assert snapshot.segments[0].status == "open"

    def test_at_version_returns_the_state_as_it_was(self, reopened):
        snapshot = reopened.get_city_snapshot(
            SIMULATION, query(SEGMENT, at_version=42)
        ).value

        assert snapshot.snapshot_version == 42
        assert snapshot.segments[0].status == "closed"

    def test_version_before_any_condition_has_no_segments(self, reopened):
        snapshot = reopened.get_city_snapshot(
            SIMULATION, query(SEGMENT, at_version=41)
        ).value

        assert snapshot.snapshot_version == 41
        assert snapshot.segments == []

    def test_future_version_is_not_found(self, reopened):
        result = reopened.get_city_snapshot(SIMULATION, query(SEGMENT, at_version=44))

        assert isinstance(result, Failure)
        assert result.kind == "NotFound"


class TestScopeAndFailures:
    def test_simulation_state_is_invisible_to_live(self, service, closure, store):
        ingest(service, closure)
        store.seed_version(LIVE, 7)

        snapshot = service.get_city_snapshot(LIVE, query(SEGMENT)).value

        assert snapshot.snapshot_version == 7
        assert snapshot.segments == []

    def test_no_city_state_yet_is_unavailable(self, service):
        result = service.get_city_snapshot(LIVE, query(SEGMENT))

        assert isinstance(result, Failure)
        assert result.kind == "Unavailable"

    def test_area_query_is_invalid_until_segments_have_geometry(self, service, closure):
        ingest(service, closure)
        area = CitySnapshotQuery.model_validate(
            {"scope": {"bbox": [-6.30, 53.33, -6.22, 53.36]}}
        )

        result = service.get_city_snapshot(SIMULATION, area)

        assert isinstance(result, Failure)
        assert result.kind == "Invalid"

    def test_store_failure_is_unavailable(self, clock, closure):
        class StoreDown(InMemoryCityStateStore):
            @contextmanager
            def read(self, state_scope: StateScope) -> Iterator[InMemoryReader]:
                raise StoreUnavailableError("database unreachable")
                yield  # pragma: no cover - makes this a generator

        service = CityMonitoring(store=StoreDown(), clock=clock)

        result = service.get_city_snapshot(SIMULATION, query(SEGMENT))

        assert isinstance(result, Failure)
        assert result.kind == "Unavailable"

    @pytest.mark.parametrize("count", [0, 201], ids=["no-keys", "too-many-keys"])
    def test_segment_key_count_must_match_the_http_contract(
        self, service, closure, count
    ):
        ingest(service, closure)
        keys = [f"seg:{n}" for n in range(1, count + 1)]

        result = service.get_city_snapshot(SIMULATION, query(*keys))

        assert isinstance(result, Failure)
        assert result.kind == "Invalid"


class TestReadsHaveNoSideEffects:
    """Regressions from the PR 44 review (C05 and Qodo)."""

    def test_ingestion_committed_during_a_read_is_kept(self, clock, closure):
        class IngestDuringRead(InMemoryCityStateStore):
            during_read: Callable[[], None] | None = None

            @contextmanager
            def read(self, state_scope: StateScope) -> Iterator[InMemoryReader]:
                with super().read(state_scope) as reader:
                    if self.during_read:
                        self.during_read()
                    yield reader

        store = IngestDuringRead()
        store.seed_version(SIMULATION, 41)
        service = CityMonitoring(store=store, clock=clock)
        ingest(service, closure)
        reopen = observation_on(
            SEGMENT,
            "open",
            OBSERVED + timedelta(minutes=5),
            "44444444-4444-4444-8444-444444444444",
        )
        store.during_read = lambda: ingest(service, reopen)

        result = service.get_city_snapshot(SIMULATION, query(SEGMENT))

        assert result.value.snapshot_version == 42
        state = store.state(SIMULATION)
        assert state.version == 43
        assert state.segments[SEGMENT].status == "open"
        assert len(state.history) == 2
        assert len(state.outbox) == 2

    def test_changing_a_returned_segment_leaves_the_store_intact(
        self, service, closure
    ):
        ingest(service, closure)

        returned = service.get_city_snapshot(SIMULATION, query(SEGMENT)).value
        returned.segments[0].status = "open"
        past = service.get_city_snapshot(
            SIMULATION, query(SEGMENT, at_version=42)
        ).value
        past.segments[0].status = "open"

        current = service.get_city_snapshot(SIMULATION, query(SEGMENT)).value
        recorded = service.get_city_snapshot(
            SIMULATION, query(SEGMENT, at_version=42)
        ).value
        assert current.segments[0].status == "closed"
        assert recorded.segments[0].status == "closed"
