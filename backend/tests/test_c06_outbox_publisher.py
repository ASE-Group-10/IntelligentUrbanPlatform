"""UC05 publishCityChanges: drain the C06 outbox to the event bus (incl. AF6)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.components.c06_city_monitoring.contracts import Observation
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.publisher import (
    PublishError,
    drain_outbox,
)
from app.components.c06_city_monitoring.store import (
    InMemoryCityStateStore,
    InMemoryOutboxBatch,
    StateScope,
    StoreUnavailableError,
)
from app.shared.contracts import EventEnvelope
from app.shared.result import Failure, Ok

RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
SIMULATION = StateScope(scope="simulation", run_id=RUN_ID)
START = datetime(2026, 11, 20, 8, 14, 30, tzinfo=UTC)


class FakeBroker:
    """Records published events; can refuse some of them like a broker outage."""

    def __init__(self, refuse: set[int] | None = None, down: bool = False) -> None:
        self.published: list[EventEnvelope] = []
        self.refuse = refuse or set()
        self.down = down
        self.attempts = 0

    def publish(self, envelope: EventEnvelope) -> None:
        self.attempts += 1
        if self.down or self.attempts in self.refuse:
            raise PublishError("broker unavailable")
        self.published.append(envelope)


def versions(events: list[EventEnvelope]) -> list[int]:
    return [e.entity_versions["citySnapshot"] for e in events]


@pytest.fixture
def store(new_store):
    return new_store()


@pytest.fixture
def ingest_closures(store):
    """Ingest ``n`` closures on different segments: City State versions 1..n."""

    def ingest(n: int) -> None:
        service = CityMonitoring(store=store, clock=lambda: START)
        context = SourceContext(state_scope=SIMULATION, correlation_id=uuid4())
        for i in range(1, n + 1):
            observation = Observation.model_validate(
                {
                    "observationId": str(uuid4()),
                    "sourceId": "sim-traffic",
                    "kind": "closure",
                    "target": {"segmentKey": f"seg:{i}"},
                    "values": {"status": "closed"},
                    "observedAt": (START + timedelta(minutes=i))
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            assert isinstance(service.ingest_observation(context, observation), Ok)

    return ingest


def test_pending_events_are_published_in_write_order(store, ingest_closures):
    ingest_closures(3)
    broker = FakeBroker()

    result = drain_outbox(store, broker)

    assert isinstance(result, Ok)
    assert result.value.published == 3
    assert result.value.stopped_by is None
    assert versions(broker.published) == [1, 2, 3]
    assert all(e.scope == "simulation" and e.run_id == RUN_ID for e in broker.published)


def test_published_events_are_not_sent_again(store, ingest_closures):
    ingest_closures(2)
    broker = FakeBroker()
    drain_outbox(store, broker)

    result = drain_outbox(store, broker)

    assert result.value.published == 0
    assert len(broker.published) == 2


def test_broker_down_keeps_events_for_later(store, ingest_closures):
    ingest_closures(3)

    down = drain_outbox(store, FakeBroker(down=True))
    recovered = FakeBroker()
    later = drain_outbox(store, recovered)

    assert down.value.published == 0
    assert "broker unavailable" in down.value.stopped_by
    assert later.value.published == 3
    assert versions(recovered.published) == [1, 2, 3]


def test_outage_mid_batch_keeps_earlier_confirmations(store, ingest_closures):
    ingest_closures(3)
    flaky = FakeBroker(refuse={2})

    first = drain_outbox(store, flaky)
    second = drain_outbox(store, FakeBroker())

    assert first.value.published == 1
    assert versions(flaky.published) == [1]
    assert second.value.published == 2


def test_batch_size_is_limited(store, ingest_closures):
    ingest_closures(5)
    broker = FakeBroker()

    result = drain_outbox(store, broker, limit=2)

    assert result.value.published == 2
    assert versions(broker.published) == [1, 2]


def test_failed_marking_republishes_with_the_same_event_id():
    """At-least-once: a confirmed event whose mark is lost is sent again."""

    class MarkLost(InMemoryOutboxBatch):
        def mark_published(self, event_id: UUID) -> None:
            raise StoreUnavailableError("connection dropped")

    class Store(InMemoryCityStateStore):
        lose_marks = True

        @contextmanager
        def outbox_batch(self, limit: int) -> Iterator[InMemoryOutboxBatch]:
            if not self.lose_marks:
                with super().outbox_batch(limit) as batch:
                    yield batch
                return
            with super().outbox_batch(limit) as batch:
                yield MarkLost(batch.events)

    store = Store()
    service = CityMonitoring(store=store, clock=lambda: START)
    context = SourceContext(state_scope=SIMULATION, correlation_id=uuid4())
    observation = Observation.model_validate(
        {
            "observationId": str(uuid4()),
            "sourceId": "sim-traffic",
            "kind": "closure",
            "target": {"segmentKey": "seg:4471023"},
            "values": {"status": "closed"},
            "observedAt": "2026-11-20T08:14:30Z",
        }
    )
    service.ingest_observation(context, observation)
    first_broker, second_broker = FakeBroker(), FakeBroker()

    first = drain_outbox(store, first_broker)
    store.lose_marks = False
    second = drain_outbox(store, second_broker)

    assert isinstance(first, Failure) and first.kind == "Unavailable"
    assert second.value.published == 1
    assert second_broker.published[0].event_id == first_broker.published[0].event_id


def test_store_down_is_unavailable():
    class StoreDown(InMemoryCityStateStore):
        @contextmanager
        def outbox_batch(self, limit: int) -> Iterator[InMemoryOutboxBatch]:
            raise StoreUnavailableError("database unreachable")
            yield  # pragma: no cover

    result = drain_outbox(StoreDown(), FakeBroker())

    assert isinstance(result, Failure)
    assert result.kind == "Unavailable"
