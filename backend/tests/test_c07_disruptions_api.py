"""UC06 ``GET /api/v1/disruptions``: C07's disruptions over HTTP."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.components.c02_identity_access import records
from app.components.c07_disruption_detection.contracts import Disruption
from app.components.c07_disruption_detection.store import (
    DisruptionStoreUnavailableError,
    InMemoryDisruptionStore,
)
from app.infrastructure.errors import APIError
from app.main import create_app

URL = "/api/v1/disruptions"
RUN_ID = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
AUTH = {"Authorization": "Bearer valid"}
CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "contracts/components/c07-disruption-detection/openapi.yaml"
)


class FakeVerifier:
    """Accepts the token ``valid`` and rejects anything else, like C02 does."""

    def subject(self, token: str) -> UUID:
        if token != "valid":
            raise APIError(401, "UNAUTHENTICATED", "Sign in to continue.")
        return uuid4()


class DownStore(InMemoryDisruptionStore):
    def disruptions(self, scope, run_id):
        raise DisruptionStoreUnavailableError("disruption store at db:5432 unreachable")


def save(store, disruption: Disruption, scope="simulation", run_id=RUN_ID) -> None:
    with store.transaction(scope, run_id) as tx:
        tx.save_disruption(disruption)


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
            "shared_run_ids": frozenset({RUN_ID}),
        },
    )()
    return TestClient(
        create_app(
            engine,
            settings,
            identity_verifier=FakeVerifier(),
            city_state_store=object(),
            disruption_store=store,
        )
    )


@pytest.fixture
def disruption(example) -> Disruption:
    return Disruption.model_validate(example("disruption.json"))


@pytest.fixture
def store() -> InMemoryDisruptionStore:
    return InMemoryDisruptionStore()


@pytest.fixture
def client(store):
    with make_client(store) as client:
        yield client


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert UUID(error["correlation_id"])


def test_run_disruption_matches_the_contract_example(
    client, store, disruption, example
):
    save(store, disruption)

    response = client.get(URL, params={"runId": str(RUN_ID)}, headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {"disruptions": [example("disruption.json")]}


def test_most_recently_updated_first(client, store, disruption):
    later = disruption.model_copy(
        update={
            "disruption_id": uuid4(),
            "updated_at": disruption.updated_at + timedelta(minutes=5),
        }
    )
    save(store, disruption)
    save(store, later)

    response = client.get(URL, params={"runId": str(RUN_ID)}, headers=AUTH)

    ids = [d["disruptionId"] for d in response.json()["disruptions"]]
    assert ids == [str(later.disruption_id), str(disruption.disruption_id)]


def test_live_and_runs_are_kept_apart(client, store, disruption):
    save(store, disruption)

    live = client.get(URL, headers=AUTH)

    assert live.status_code == 200, live.text
    assert live.json() == {"disruptions": []}


def test_run_without_disruptions_is_an_empty_list(client):
    response = client.get(URL, params={"runId": str(RUN_ID)}, headers=AUTH)

    assert response.json() == {"disruptions": []}


def test_requires_sign_in(client):
    assert_error(client.get(URL), 401, "UNAUTHENTICATED")


def test_run_that_is_not_shared_is_forbidden(client, store, disruption):
    other_run = uuid4()
    save(store, disruption, run_id=other_run)

    response = client.get(URL, params={"runId": str(other_run)}, headers=AUTH)

    assert_error(response, 403, "Forbidden")
    assert str(disruption.disruption_id) not in response.text


def test_malformed_run_id_is_invalid(client):
    assert_error(
        client.get(URL, params={"runId": "not-a-uuid"}, headers=AUTH), 400, "Invalid"
    )


def test_store_down_is_unavailable_without_details():
    with make_client(DownStore()) as client:
        response = client.get(URL, params={"runId": str(RUN_ID)}, headers=AUTH)

    assert_error(response, 503, "Unavailable")
    assert "db:5432" not in response.text


def test_generated_openapi_matches_the_contract(client):
    """C01 generates its client from /openapi.json, so it must match the contract."""
    expected = yaml.safe_load(CONTRACT.read_text())["paths"]["/api/v1/disruptions"][
        "get"
    ]
    generated = client.get("/openapi.json").json()["paths"]["/api/v1/disruptions"][
        "get"
    ]

    def comparable(parameter: dict) -> dict:
        keep = ("name", "in", "required", "schema")
        return {k: parameter[k] for k in keep if k in parameter}

    assert generated["operationId"] == expected["operationId"]
    assert [comparable(p) for p in generated["parameters"]] == [
        comparable(p) for p in expected["parameters"]
    ]
    assert set(generated["responses"]) == set(expected["responses"])
