"""UC05 ``GET /api/v1/city-state/snapshot``: the C06 snapshot read over HTTP."""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.components.c02_identity_access import records
from app.components.c06_city_monitoring.contracts import CitySnapshot, Observation
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.router import libpq_url
from app.components.c06_city_monitoring.store import (
    InMemoryCityStateStore,
    StateScope,
    StoreUnavailableError,
)
from app.infrastructure.errors import APIError
from app.main import create_app
from app.shared.result import Ok

URL = "/api/v1/city-state/snapshot"
SEGMENT = "seg:4471023"
RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
LIVE = StateScope(scope="live")
SIMULATION = StateScope(scope="simulation", run_id=RUN_ID)
NOW = datetime(2026, 11, 20, 8, 14, 35, tzinfo=UTC)
AUTH = {"Authorization": "Bearer valid"}
CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "contracts/components/c06-city-monitoring/openapi.yaml"
)


class FakeVerifier:
    """Accepts the token ``valid`` and rejects anything else, like C02 does."""

    def subject(self, token: str) -> UUID:
        if token != "valid":
            raise APIError(401, "UNAUTHENTICATED", "Sign in to continue.")
        return uuid4()


class DownStore(InMemoryCityStateStore):
    @contextmanager
    def read(self, state_scope):
        raise StoreUnavailableError("City State store unreachable")
        yield


@pytest.fixture
def store() -> InMemoryCityStateStore:
    store = InMemoryCityStateStore()
    store.seed_version(LIVE, 41)
    return store


def make_client(store) -> TestClient:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    ).execution_options(schema_translate_map={"c02_identity_access": None})
    records.metadata.create_all(engine)
    settings = type(
        "Settings",
        (),
        {
            "database_url": SecretStr("sqlite://"),
            "mock_dependencies": True,
            "supabase_url": None,
            "availability_max_age_seconds": 86400,
            "c06_shared_run_ids": frozenset({RUN_ID}),
        },
    )()
    return TestClient(
        create_app(
            engine,
            settings,
            identity_verifier=FakeVerifier(),
            city_state_store=store,
            clock=lambda: NOW,
        )
    )


@pytest.fixture
def client(store):
    with make_client(store) as client:
        yield client


def ingest(store, observation: dict, scope: StateScope = LIVE) -> None:
    service = CityMonitoring(store=store, clock=lambda: NOW)
    context = SourceContext(state_scope=scope, correlation_id=uuid4())
    result = service.ingest_observation(
        context, Observation.model_validate(observation)
    )
    assert isinstance(result, Ok)


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert UUID(error["correlation_id"])


def test_closure_matches_contract_example(client, store, example):
    ingest(store, example("observation-closure.json"))

    response = client.get(URL, params={"segmentKeys": SEGMENT}, headers=AUTH)

    assert response.status_code == 200, response.text
    assert CitySnapshot.model_validate(response.json()) == CitySnapshot.model_validate(
        example("city-snapshot.json")
    )
    assert response.json()["segments"][0]["segmentKey"] == SEGMENT


def test_comma_separated_keys_return_only_recorded_segments(client, store, example):
    ingest(store, example("observation-closure.json"))

    response = client.get(
        URL, params={"segmentKeys": f"seg:1,{SEGMENT},seg:1"}, headers=AUTH
    )

    assert response.status_code == 200, response.text
    assert [s["segmentKey"] for s in response.json()["segments"]] == [SEGMENT]


def test_old_values_come_back_degraded_with_max_age(client, store, example):
    ingest(store, example("observation-closure.json"))

    response = client.get(
        URL, params={"segmentKeys": SEGMENT, "maxAgeSeconds": 1}, headers=AUTH
    )

    body = response.json()
    assert body["segments"][0]["quality"] == "degraded"
    assert body["staleSources"] == ["sim-traffic"]
    assert body["overallQuality"] == "degraded"


def test_past_version_and_run_scope(client, store, example):
    ingest(store, example("observation-closure.json"))
    store.seed_version(SIMULATION, 7)

    past = client.get(
        URL, params={"segmentKeys": SEGMENT, "atVersion": 41}, headers=AUTH
    )
    run = client.get(
        URL, params={"segmentKeys": SEGMENT, "runId": str(RUN_ID)}, headers=AUTH
    )

    assert past.json()["snapshotVersion"] == 41
    assert past.json()["segments"] == []
    assert run.json()["snapshotVersion"] == 7
    assert run.json()["segments"] == []


def test_requires_sign_in(client):
    assert_error(
        client.get(URL, params={"segmentKeys": SEGMENT}), 401, "UNAUTHENTICATED"
    )
    assert_error(
        client.get(
            URL,
            params={"segmentKeys": SEGMENT},
            headers={"Authorization": "Bearer forged"},
        ),
        401,
        "UNAUTHENTICATED",
    )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"segmentKeys": ""},
        {"segmentKeys": "4471023"},
        {"segmentKeys": "seg:0"},
        {"segmentKeys": ",".join(f"seg:{n}" for n in range(1, 202))},
        {"segmentKeys": SEGMENT, "maxAgeSeconds": 0},
        {"segmentKeys": SEGMENT, "atVersion": "latest"},
        {"segmentKeys": SEGMENT, "runId": "not-a-uuid"},
    ],
)
def test_invalid_parameters(client, params):
    assert_error(client.get(URL, params=params, headers=AUTH), 400, "Invalid")


def test_version_newer_than_current_is_not_found(client):
    response = client.get(
        URL, params={"segmentKeys": SEGMENT, "atVersion": 42}, headers=AUTH
    )
    assert_error(response, 404, "NotFound")


def test_no_city_state_yet_is_unavailable():
    with make_client(InMemoryCityStateStore()) as client:
        response = client.get(URL, params={"segmentKeys": SEGMENT}, headers=AUTH)

    assert_error(response, 503, "Unavailable")


def test_run_that_is_not_shared_is_forbidden(client, store):
    other_run = uuid4()
    store.seed_version(StateScope(scope="simulation", run_id=other_run), 3)

    response = client.get(
        URL, params={"segmentKeys": SEGMENT, "runId": str(other_run)}, headers=AUTH
    )

    assert_error(response, 403, "Forbidden")
    assert "snapshotVersion" not in response.text


def test_max_age_beyond_timedelta_range_treats_nothing_as_stale(client, store, example):
    ingest(store, example("observation-closure.json"))

    response = client.get(
        URL, params={"segmentKeys": SEGMENT, "maxAgeSeconds": 10**18}, headers=AUTH
    )

    assert response.status_code == 200, response.text
    assert response.json()["segments"][0]["quality"] == "good"


def test_generated_openapi_matches_the_contract(client):
    """C01 generates its client from /openapi.json, so it must match the contract."""
    contract = yaml.safe_load(CONTRACT.read_text())
    expected = contract["paths"]["/api/v1/city-state/snapshot"]["get"]
    segment_key = contract["components"]["schemas"]["C06SegmentKey"]
    generated = client.get("/openapi.json").json()["paths"][
        "/api/v1/city-state/snapshot"
    ]["get"]

    def comparable(parameter: dict) -> dict:
        schema = dict(parameter["schema"])
        if "items" in schema:
            schema["items"] = {"type": "string", "pattern": segment_key["pattern"]}
        keep = ("name", "in", "required", "style", "explode")
        return {**{k: parameter[k] for k in keep if k in parameter}, "schema": schema}

    assert generated["operationId"] == expected["operationId"]
    assert [comparable(p) for p in generated["parameters"]] == [
        comparable(p) for p in expected["parameters"]
    ]
    assert set(generated["responses"]) == set(expected["responses"])
    error = generated["responses"]["401"]["content"]["application/json"]["schema"]
    assert error == {"$ref": "#/components/schemas/C06ErrorResponse"}


def test_store_down_is_unavailable_without_details():
    with make_client(DownStore()) as client:
        response = client.get(URL, params={"segmentKeys": SEGMENT}, headers=AUTH)

    assert_error(response, 503, "Unavailable")
    assert "unreachable" not in response.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://u:p@db:5432/ulop", "postgresql://u:p@db:5432/ulop"),
        (
            "postgresql+psycopg://u:p@db:5432/ulop?sslmode=require",
            "postgresql://u:p@db:5432/ulop?sslmode=require",
        ),
    ],
)
def test_libpq_url_drops_the_sqlalchemy_driver(url, expected):
    assert libpq_url(url) == expected
