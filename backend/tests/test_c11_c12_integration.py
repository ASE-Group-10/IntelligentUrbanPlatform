"""Persistent adapters, actual HTTP routing and broker boundary checks.

SQLite is a portable routine check; the same adapter tests run on the dedicated
PostgreSQL service in CI. Authentication dependencies are replaced by explicit
trusted contexts in these API tests; existing C02 tests cover token verification.
"""

from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from app.components.c11_kpi_analytics import router as c11_router
from app.components.c11_kpi_analytics.contracts import KPIQuery
from app.components.c11_kpi_analytics.store import (
    DEFINITION,
    ObservationSource,
    ObservationStore,
    observations,
)
from app.components.c11_kpi_analytics.store import (
    metadata as c11_metadata,
)
from app.components.c12_simulation_replay import router as c12_router
from app.components.c12_simulation_replay.contracts import ReplayControl
from app.components.c12_simulation_replay.persistence import (
    ReplayRepository,
    encode,
)
from app.components.c12_simulation_replay.persistence import (
    metadata as c12_metadata,
)
from app.components.c12_simulation_replay.recording import CaptureRecorder
from app.infrastructure.errors import APIError, api_error_handler
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext
from app.shared.contracts import EventEnvelope
from app.shared.events import handle_delivery
from tests.test_c12_replay import setup

replay_fixture = setup


@pytest.fixture(params=["sqlite", "postgres"])
def integration_engine(request):
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        ).execution_options(
            schema_translate_map={
                "c11_kpi_analytics": None,
                "c12_simulation_replay": None,
            }
        )
        c11_metadata.create_all(engine)
        c12_metadata.create_all(engine)
    else:
        url = request.getfixturevalue("test_database_url")
        engine = create_engine(url.replace("postgres://", "postgresql+psycopg://"))
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE c11_kpi_analytics.observations, "
                    "c11_kpi_analytics.delivery_bindings, "
                    "c11_kpi_analytics.page_cursors, "
                    "c12_simulation_replay.captures, "
                    "c12_simulation_replay.run_page_cursors, "
                    "c12_simulation_replay.command_receipts, "
                    "c12_simulation_replay.sessions, c12_simulation_replay.runs"
                )
            )
    yield engine
    engine.dispose()


@pytest.fixture
def context():
    return AuthContext(
        principal_id=uuid4(),
        principal_type="user",
        acting_provider_id=UUID("22222222-2222-4222-8222-222222222222"),
        provider_ids=[UUID("22222222-2222-4222-8222-222222222222")],
        roles=["analyst"],
        platform_roles=[],
        correlation_id=uuid4(),
    )


def source_event():
    import json

    path = (
        Path(__file__).resolve().parents[2]
        / "contracts/components/c04-delivery-management/examples"
        / "delivery-progress-changed-event.json"
    )
    return EventEnvelope.model_validate(json.loads(path.read_text()))


def query(context):
    return KPIQuery.model_validate(
        {
            "scope": {
                "providerId": str(context.acting_provider_id),
                "scope": "live",
                "period": {
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2027-01-01T00:00:00Z",
                },
            },
            "metricDefinitionVersion": DEFINITION,
        }
    )


def bind(store, event, context):
    store.bind_delivery(
        context.acting_provider_id,
        UUID(event.payload["deliveryId"]),
        event.scope,
        event.run_id,
    )


def test_unbound_data_is_invisible_and_repeated_events_are_idempotent(
    integration_engine, context
):
    event = source_event()
    store = ObservationStore(integration_engine)
    source = ObservationSource(integration_engine, context.principal_id)
    assert handle_delivery(event.model_dump_json().encode(), store.consume) == "ack"
    assert store.consume(event) == "ack"
    assert (
        source.breakdown(query(context), "delivery-time", 20, None).value.records == []
    )
    bind(store, event, context)
    page = source.breakdown(query(context), "delivery-time", 20, None).value
    assert len(page.records) == 1 and page.source_event_ids == [event.event_id]
    conflict = event.model_copy(deep=True)
    conflict.payload["delivery"]["progress"]["status"] = "failed"
    assert store.consume(conflict) == "reject"
    with integration_engine.connect() as conn:
        assert len(conn.execute(select(observations)).all()) == 1
    indicators = source.query(query(context)).value.indicators
    assert all(i.value is None and i.status == "unavailable" for i in indicators)


def test_cursor_is_bound_to_actor_filters_and_snapshot(integration_engine, context):
    store = ObservationStore(integration_engine)
    source = ObservationSource(integration_engine, context.principal_id)
    event = source_event()
    bind(store, event, context)
    assert store.consume(event) == "ack"
    second = event.model_copy(deep=True)
    second.event_id = uuid4()
    second.payload["deliveryVersion"] += 1
    second.payload["delivery"]["version"] += 1
    assert store.consume(second) == "ack"
    page = source.breakdown(query(context), "delivery-time", 1, None).value
    assert page.next_cursor
    outsider = ObservationSource(integration_engine, uuid4())
    assert (
        outsider.breakdown(query(context), "delivery-time", 1, page.next_cursor).kind
        == "Invalid"
    )
    assert (
        source.breakdown(query(context), "cost", 1, page.next_cursor).kind == "Invalid"
    )
    third = second.model_copy(deep=True)
    third.event_id = uuid4()
    third.payload["deliveryVersion"] += 1
    third.payload["delivery"]["version"] += 1
    assert store.consume(third) == "ack"
    next_page = source.breakdown(
        query(context), "delivery-time", 1, page.next_cursor
    ).value
    assert next_page.next_cursor is None
    assert set(next_page.source_event_ids).isdisjoint(page.source_event_ids)


def test_provider_binding_and_run_scope_cannot_widen_reads(integration_engine, context):
    store = ObservationStore(integration_engine)
    event = source_event()
    bind(store, event, context)
    with pytest.raises(ValueError):
        store.bind_delivery(
            uuid4(), UUID(event.payload["deliveryId"]), event.scope, event.run_id
        )
    assert store.consume(event) == "ack"
    q = query(context)
    q.scope.provider_id = uuid4()
    assert (
        ObservationSource(integration_engine)
        .breakdown(q, "cost", 20, None)
        .value.records
        == []
    )
    q = query(context)
    q.scope.scope, q.scope.run_id = "simulation", uuid4()
    assert (
        ObservationSource(integration_engine)
        .breakdown(q, "cost", 20, None)
        .value.records
        == []
    )


def test_replay_survives_repository_restart_and_checks_idempotency(
    integration_engine, context, replay_fixture
):
    _, reader, request, _, _ = replay_fixture
    archive = deepcopy(reader.archive)
    archive.manifest.provider_id = context.acting_provider_id
    repo = ReplayRepository(integration_engine)
    data = encode(archive)
    config = {"version": archive.manifest.configuration_version, "synthetic": True}
    import hashlib
    import json

    data["configuration"] = config
    data["configurationSha256"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    repo.import_archive(data)
    key = str(uuid4())
    frame = repo.start(context, request, key).value
    assert repo.start(context, request, key).value == frame
    changed = request.model_copy(update={"speed": 2})
    assert repo.start(context, changed, key).kind == "Conflict"
    control = ReplayControl(
        session_id=frame.session_id, command="play", expected_cursor=0
    )
    assert (
        repo.operate(context, frame.session_id, control, str(uuid4())).value.status
        == "playing"
    )
    restarted = ReplayRepository(integration_engine)
    advanced = restarted.operate(context, frame.session_id, tick=0).value
    assert advanced.cursor == 1
    assert restarted.operate(context, frame.session_id, tick=0).kind == "Conflict"
    assert restarted.operate(context, frame.session_id).value.cursor == 1
    foreign = context.model_copy(update={"principal_id": uuid4()})
    assert restarted.operate(foreign, frame.session_id).kind == "NotFound"
    other_provider = context.model_copy(update={"acting_provider_id": uuid4()})
    assert restarted.operate(other_provider, frame.session_id).kind == "NotFound"


def client(engine, context):
    app = FastAPI()
    app.state.engine = engine
    app.state.replay_engine = engine  # SQL role isolation is tested on Postgres below.
    app.state.settings = type("Settings", (), {"c06_shared_run_ids": ()})()
    app.dependency_overrides[authenticate] = lambda: context

    @app.middleware("http")
    async def trace(request, call_next):
        request.state.correlation_id = str(uuid4())
        return await call_next(request)

    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(c11_router.router)
    app.include_router(c12_router.router)
    return TestClient(app)


def test_http_uses_c02_scope_and_rejects_wrong_roles(integration_engine, context):
    with client(integration_engine, context) as http:
        body = query(context).model_dump(mode="json", by_alias=True)
        response = http.post("/api/v1/analytics/kpis/query", json=body)
        assert response.status_code == 200 and response.json()["status"] == "partial"
        body["scope"]["providerId"] = str(uuid4())
        assert http.post("/api/v1/analytics/kpis/query", json=body).status_code == 403
    driver = context.model_copy(update={"roles": ("driver",)})
    with client(integration_engine, driver) as http:
        assert (
            http.get(
                "/api/v1/simulation-runs",
                params={"providerId": str(context.acting_provider_id)},
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/api/v1/analytics/kpis/query",
                json=query(context).model_dump(mode="json", by_alias=True),
            ).status_code
            == 403
        )


def test_capture_is_durable_scoped_and_explicitly_partial(integration_engine, context):
    from datetime import timedelta

    recorder = CaptureRecorder(integration_engine)
    event = source_event().model_copy(deep=True)
    event.scope, event.run_id = "simulation", uuid4()
    start = event.observed_at - timedelta(minutes=1)
    config = {"initialState": {"deliveries": {}}, "scenario": "synthetic-C04-only"}
    recorder.begin(event.run_id, context.acting_provider_id, start, config, True)
    assert recorder.consume(event) == "ack"
    assert CaptureRecorder(integration_engine).consume(event) == "ack"
    recorder.seal(event.run_id, event.observed_at + timedelta(minutes=1))
    repo = ReplayRepository(integration_engine)
    page = repo.list_runs(context).value
    manifest = page["items"][0]
    assert manifest["status"] == "incomplete" and manifest["lastSequence"] == 1
    from app.components.c12_simulation_replay.contracts import ReplayRequest

    request = ReplayRequest(
        run_id=event.run_id, time_range=manifest["availableRanges"][0]
    )
    frame = repo.start(context, request, str(uuid4())).value
    command = ReplayControl(
        session_id=frame.session_id, command="play", expected_cursor=0
    )
    repo.operate(context, frame.session_id, command, str(uuid4()))
    next_frame = (
        ReplayRepository(integration_engine)
        .operate(context, frame.session_id, tick=0)
        .value
    )
    assert next_frame.state["recordingScope"] == "C04 delivery outcomes only"
    assert next_frame.state["synthetic"] is True
    assert event.payload["deliveryId"] in next_frame.state["deliveries"]
    assert recorder.consume(source_event()) == "reject"  # live is never recorded


def test_capture_with_conflicting_history_cannot_be_sealed(integration_engine, context):
    from datetime import timedelta

    recorder = CaptureRecorder(integration_engine)
    event = source_event().model_copy(deep=True)
    event.scope, event.run_id = "simulation", uuid4()
    recorder.begin(
        event.run_id,
        context.acting_provider_id,
        event.observed_at - timedelta(minutes=1),
        {"initialState": {}},
        True,
    )
    assert recorder.consume(event) == "ack"
    altered = event.model_copy(deep=True)
    altered.payload["delivery"]["progress"]["status"] = "failed"
    assert recorder.consume(altered) == "reject"
    with pytest.raises(ValueError, match="invalid or missing"):
        recorder.seal(event.run_id, event.observed_at + timedelta(minutes=1))


def test_replay_http_start_controls_and_provider_filter(
    integration_engine, context, replay_fixture
):
    import hashlib
    import json

    _, reader, body, _, _ = replay_fixture
    archive = deepcopy(reader.archive)
    archive.manifest.provider_id = context.acting_provider_id
    data = encode(archive)
    config = {"version": archive.manifest.configuration_version, "synthetic": True}
    data["configuration"] = config
    data["configurationSha256"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    ReplayRepository(integration_engine).import_archive(data)
    with client(integration_engine, context) as http:
        url = "/api/v1/simulation-runs"
        assert http.get(url).status_code == 422
        assert (
            http.get(
                url,
                params={
                    "providerId": str(context.acting_provider_id),
                    "cursor": "",
                },
            ).status_code
            == 422
        )
        assert (
            http.get(
                "/api/v1/replay-sessions/00000000-0000-1000-8000-000000000001"
            ).status_code
            == 422
        )
        assert http.get(url, params={"providerId": str(uuid4())}).status_code == 403
        page = http.get(
            url, params={"providerId": str(context.acting_provider_id)}
        ).json()
        assert len(page["items"]) == 1
        assert (
            http.get(
                url,
                params={
                    "providerId": str(context.acting_provider_id),
                    "start": "2026-01-01T00:00:00Z",
                },
            ).status_code
            == 422
        )
        headers = {"Idempotency-Key": str(uuid4())}
        result = http.post(
            "/api/v1/replay-sessions",
            json=body.model_dump(mode="json", by_alias=True),
            headers=headers,
        )
        assert result.status_code == 201 and "location" in result.headers
        assert (
            http.post(
                "/api/v1/replay-sessions",
                json=body.model_dump(mode="json", by_alias=True),
                headers=headers,
            ).status_code
            == 200
        )
        frame_url = result.headers["location"]
        assert http.get(frame_url).json()["cursor"] == 0
        play = http.post(
            frame_url + "/controls",
            json={"command": "play", "expectedCursor": 0},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert play.status_code == 200
        assert (
            http.post(frame_url + "/advance", json={"expectedCursor": 0}).json()[
                "cursor"
            ]
            == 1
        )
        assert (
            http.post(frame_url + "/advance", json={"expectedCursor": 0}).status_code
            == 409
        )
        assert http.get(frame_url).json()["cursor"] == 1
