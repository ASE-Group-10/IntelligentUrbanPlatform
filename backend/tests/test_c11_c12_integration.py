"""Persistent adapters, actual HTTP routing and broker boundary checks.

SQLite is a portable routine check; the same adapter tests run on the dedicated
PostgreSQL service in CI. Authentication dependencies are replaced by explicit
trusted contexts in these API tests; existing C02 tests cover token verification.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
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


def source_event(*, provider_id=None):
    import json

    path = (
        Path(__file__).resolve().parents[2]
        / "contracts/components/c04-delivery-management/examples"
        / "delivery-progress-changed-event.json"
    )
    raw = json.loads(path.read_text())
    # Keep explicit legacy fixtures independent of the producer's #126 examples.
    raw["payload"].pop("providerId", None)
    if provider_id is not None:
        raw["payload"]["providerId"] = str(provider_id)
    return EventEnvelope.model_validate(raw)


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


def test_c04_provider_binds_atomically_and_cannot_be_reassigned(
    integration_engine, context
):
    event = source_event(provider_id=context.acting_provider_id)
    store = ObservationStore(integration_engine)
    assert store.consume(event) == store.consume(event) == "ack"
    page = (
        ObservationSource(integration_engine)
        .breakdown(query(context), "delivery-time", 20, None)
        .value
    )
    assert page.source_event_ids == [event.event_id]
    other = event.model_copy(deep=True)
    other.event_id = uuid4()
    other.payload["providerId"] = str(uuid4())
    assert store.consume(other) == "reject"
    invalid = event.model_copy(deep=True)
    invalid.event_id = uuid4()
    invalid.payload["providerId"] = "not-a-provider"
    assert store.consume(invalid) == "reject"
    with integration_engine.connect() as conn:
        assert conn.execute(select(observations.c.event_id)).scalars().all() == [
            event.event_id
        ]


def test_c12_shared_run_does_not_capture_another_providers_delivery(
    integration_engine, context
):
    from app.components.c12_simulation_replay.persistence import runs

    event = source_event(provider_id=context.acting_provider_id)
    event.scope, event.run_id = "simulation", uuid4()
    recorder = CaptureRecorder(integration_engine)
    recorder.begin(
        event.run_id,
        context.acting_provider_id,
        event.observed_at - timedelta(minutes=1),
        {"initialState": {}},
        True,
    )
    foreign = event.model_copy(deep=True)
    foreign.event_id = uuid4()
    foreign.payload["providerId"] = str(uuid4())
    assert recorder.consume(foreign) == "ack"
    assert recorder.consume(event) == "ack"
    recorder.seal(event.run_id, event.observed_at + timedelta(minutes=1))
    with integration_engine.connect() as conn:
        archive = conn.execute(select(runs.c.archive)).scalar_one()
    assert len(archive["steps"]) == 1
    assert archive["steps"][0]["record"]["event"]["event_id"] == str(event.event_id)


@pytest.mark.parametrize("same_provider", [False, True])
@pytest.mark.parametrize("malformed_delivery", [None, {}])
def test_capture_validates_details_only_after_provider_isolation(
    integration_engine, context, same_provider, malformed_delivery
):
    from app.components.c12_simulation_replay.persistence import runs

    event = source_event(provider_id=context.acting_provider_id)
    event.scope, event.run_id = "simulation", uuid4()
    recorder = CaptureRecorder(integration_engine)
    recorder.begin(
        event.run_id,
        context.acting_provider_id,
        event.observed_at - timedelta(minutes=1),
        {"initialState": {}},
        True,
    )
    malformed = event.model_copy(deep=True)
    malformed.event_id = uuid4()
    if not same_provider:
        malformed.payload["providerId"] = str(uuid4())
    malformed.payload["delivery"] = malformed_delivery
    end = event.observed_at + timedelta(minutes=1)
    if same_provider:
        assert recorder.consume(malformed) == "reject"
        with pytest.raises(ValueError, match="invalid or missing"):
            recorder.seal(event.run_id, end)
    else:
        assert recorder.consume(malformed) == "ack"
        assert recorder.consume(event) == "ack"
        recorder.seal(event.run_id, end)
        with integration_engine.connect() as conn:
            archive = conn.execute(select(runs.c.archive)).scalar_one()
        assert len(archive["steps"]) == 1
        assert archive["steps"][0]["record"]["event"]["event_id"] == str(event.event_id)


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


def client(engine, context, replay_engine=None):
    app = FastAPI()
    app.state.engine = engine
    app.state.replay_engine = replay_engine if replay_engine is not None else engine
    app.state.settings = type("Settings", (), {"shared_run_ids": ()})()
    app.dependency_overrides[authenticate] = lambda: context

    @app.middleware("http")
    async def trace(request, call_next):
        request.state.correlation_id = str(uuid4())
        return await call_next(request)

    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(c11_router.router)
    app.include_router(c12_router.router)
    return TestClient(app)


def test_synthetic_trace_verifies_http_and_fails_if_a_worker_is_missing(
    integration_engine, context
):
    from app.components.analytics_demo import fixture_events, wait_for_recording
    from app.components.analytics_verify import verify_http

    now = datetime.now(UTC)
    run_id = uuid4()
    events = fixture_events(
        run_id, uuid4(), context.correlation_id, now, context.acting_provider_id
    )
    assert {e.correlation_id for e in events} == {context.correlation_id}
    assert events[1].causation_id == events[0].event_id
    recorder = CaptureRecorder(integration_engine)
    recorder.begin(
        run_id,
        context.acting_provider_id,
        now - timedelta(minutes=2),
        {"initialState": {"deliveries": {}}},
        True,
    )
    store = ObservationStore(integration_engine)
    for event in events:
        assert store.consume(event) == "ack"
    with pytest.raises(RuntimeError, match="worker evidence missing"):
        wait_for_recording(integration_engine, run_id, events, timeout=0)
    for event in events:
        assert recorder.consume(event) == "ack"
    wait_for_recording(integration_engine, run_id, events, timeout=0)
    recorder.seal(run_id, now + timedelta(minutes=1))
    with client(integration_engine, context) as http:
        http.app.state.settings.shared_run_ids = (run_id,)

        def call(method, path, body=None, key=None):
            response = http.request(
                method,
                path,
                json=body,
                headers={"Idempotency-Key": key} if key else {},
            )
            assert response.is_success, response.text
            return response.json()

        verify_http(call, context.acting_provider_id, run_id, events, now)


@pytest.mark.parametrize(
    "url,token",
    [
        ("https://example.com", "synthetic-test-token"),
        ("http://localhost@other.invalid", "synthetic-test-token"),
        ("http://localhost?forward=other", "synthetic-test-token"),
        ("http://localhost", ""),
    ],
)
def test_demo_verifier_refuses_remote_destinations_and_missing_identity(url, token):
    from app.components.analytics_verify import LocalAPI

    with pytest.raises(ValueError):
        LocalAPI(url, token, uuid4(), uuid4())


def test_app_keeps_disruptions_analytics_and_replay_routes_after_merge():
    from app.main import create_app

    paths = create_app().openapi()["paths"]
    assert "post" in paths["/api/v1/deliveries"]
    assert "post" in paths["/api/v1/plans/{planId}/confirmation"]
    assert "get" in paths["/api/v1/disruptions"]
    assert "post" in paths["/api/v1/analytics/kpis/query"]
    assert "get" in paths["/api/v1/simulation-runs"]
    assert "post" in paths["/api/v1/replay-sessions"]


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
    event = source_event(provider_id=context.acting_provider_id).model_copy(deep=True)
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
    event = source_event(provider_id=context.acting_provider_id).model_copy(deep=True)
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
