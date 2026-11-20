"""Postgres-only behaviour of the C06 store: concurrency and connection failures.

Everything else is covered by running the ingestion and snapshot tests against
both stores (see the ``new_store`` fixture in ``conftest.py``).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.components.c06_city_monitoring.contracts import CitySnapshotQuery, Observation
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c06_city_monitoring.store import StateScope
from app.shared.result import Failure, Ok

RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
SIMULATION = StateScope(scope="simulation", run_id=RUN_ID)
START = datetime(2026, 11, 20, 8, 14, 30, tzinfo=UTC)


def closure_on(segment: str, minutes: int) -> Observation:
    return Observation.model_validate(
        {
            "observationId": str(uuid4()),
            "sourceId": "sim-traffic",
            "kind": "closure",
            "target": {"segmentKey": segment},
            "values": {"status": "closed"},
            "observedAt": (START + timedelta(minutes=minutes))
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )


def test_concurrent_ingestions_get_distinct_versions(c06_database):
    service = CityMonitoring(
        store=PostgresCityStateStore(c06_database), clock=lambda: START
    )
    context = SourceContext(state_scope=SIMULATION, correlation_id=uuid4())
    observations = [closure_on(f"seg:{n}", n) for n in range(1, 21)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda o: service.ingest_observation(context, o), observations)
        )

    assert all(isinstance(r, Ok) for r in results)
    versions = sorted(r.value.snapshot_version for r in results)
    assert versions == list(range(1, 21))
    store = PostgresCityStateStore(c06_database)
    state = store.state(SIMULATION)
    assert state.version == 20
    assert len(state.outbox) == 20


def test_unreachable_database_is_unavailable():
    store = PostgresCityStateStore(
        "postgres://ulop:ulop@127.0.0.1:1/ulop_test?connect_timeout=2"
    )
    service = CityMonitoring(store=store, clock=lambda: START)
    context = SourceContext(state_scope=SIMULATION, correlation_id=uuid4())

    ingested = service.ingest_observation(context, closure_on("seg:1", 0))
    read = service.get_city_snapshot(
        SIMULATION,
        CitySnapshotQuery.model_validate({"scope": {"segmentKeys": ["seg:1"]}}),
    )

    assert isinstance(ingested, Failure) and ingested.kind == "Unavailable"
    assert isinstance(read, Failure) and read.kind == "Unavailable"


@pytest.mark.usefixtures("c06_database")
def test_outbox_keeps_events_in_write_order(c06_database):
    service = CityMonitoring(
        store=PostgresCityStateStore(c06_database), clock=lambda: START
    )
    context = SourceContext(state_scope=SIMULATION, correlation_id=uuid4())
    for n in range(1, 6):
        service.ingest_observation(context, closure_on(f"seg:{n}", n))

    outbox = PostgresCityStateStore(c06_database).state(SIMULATION).outbox

    assert [e.entity_versions["citySnapshot"] for e in outbox] == [1, 2, 3, 4, 5]
