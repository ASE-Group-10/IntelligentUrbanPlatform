from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.infrastructure.database import audit, metadata, outbox, receipts, resources
from app.main import create_app
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

BASE = "/api/v1/resources"
PAYLOAD = {
    "external_identifier": "VAN-001",
    "name": "Test van",
    "resource_type": "vehicle",
    "address": "Dublin Depot (synthetic)",
    "latitude": 53.3498,
    "longitude": -6.2603,
    "observed_availability": "available",
    "details": {
        "vehicle_category": "van",
        "max_weight_kg": 800,
        "powertrain": "electric",
        "autonomous": False,
    },
}


def headers(token="dev-manager", **extra):
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid4()),
        **extra,
    }


@pytest.fixture
def setup():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata.create_all(engine)
    settings = SimpleNamespace(
        database_url=SecretStr("sqlite://"),
        mock_dependencies=True,
        availability_max_age_seconds=86400,
    )
    with TestClient(create_app(engine, settings)) as client:
        yield client, engine
    engine.dispose()


def count(engine, table):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table))


def test_create_update_read_and_version_conflict(setup):
    client, engine = setup
    response = client.post(BASE, headers=headers(), json=PAYLOAD)
    assert response.status_code == 201, response.text
    resource = response.json()
    assert resource["version"] == 1 and resource["planning_eligible"]
    assert resource["details"]["timezone"] == "Europe/Dublin"
    assert response.headers["location"] == f"{BASE}/{resource['id']}"
    path = f"{BASE}/{resource['id']}"
    changed = client.post(
        path, headers=headers(), json={"expected_version": 1, "name": "New name"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    stale = client.post(
        path, headers=headers(), json={"expected_version": 1, "name": "Stale"}
    )
    assert stale.status_code == 409
    assert client.get(path, headers=headers()).json()["name"] == "New name"
    assert count(engine, audit) == 2
    assert count(engine, receipts) == 2


def test_idempotency_duplicate_identifier_and_atomic_records(setup):
    client, engine = setup
    request_headers = headers()
    first = client.post(BASE, headers=request_headers, json=PAYLOAD)
    second = client.post(BASE, headers=request_headers, json=PAYLOAD)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["idempotency-replayed"] == "true"
    conflict = client.post(
        BASE, headers=request_headers, json={**PAYLOAD, "name": "Changed"}
    )
    assert conflict.status_code == 409
    duplicate = client.post(
        BASE, headers=headers(), json={**PAYLOAD, "external_identifier": " van-001 "}
    )
    assert duplicate.status_code == 409
    assert (
        count(engine, resources) == count(engine, receipts) == count(engine, audit) == 1
    )
    assert count(engine, outbox) == 2


@pytest.mark.parametrize("scenario,code", [("unmatched", 422), ("unconfirmed", 503)])
def test_mapping_failure_stores_nothing(setup, scenario, code):
    client, engine = setup
    response = client.post(
        BASE, headers=headers(**{"X-Mock-Location-Result": scenario}), json=PAYLOAD
    )
    assert response.status_code == code
    for table in [resources, receipts, audit, outbox]:
        assert count(engine, table) == 0


def test_failed_activation_retry_and_already_active(setup):
    client, engine = setup
    response = client.post(
        BASE, headers=headers(**{"X-Mock-Agent-Result": "failed"}), json=PAYLOAD
    )
    assert response.status_code == 201
    failed = response.json()
    assert failed["agent_status"] == "failed" and not failed["planning_eligible"]
    path = f"{BASE}/{failed['id']}/agent-activation"
    request_headers = headers()
    activated = client.post(path, headers=request_headers, json={"expected_version": 1})
    assert activated.status_code == 200, activated.text
    active = activated.json()
    assert active["agent_id"] and active["agent_status"] == "active"
    repeated = client.post(path, headers=request_headers, json={"expected_version": 1})
    assert repeated.json() == active
    again = client.post(path, headers=headers(), json={"expected_version": 2})
    assert again.json()["version"] == 2
    assert again.json()["agent_id"] == active["agent_id"]


def test_pending_activation_and_deactivation(setup):
    client, _ = setup
    created = client.post(
        BASE, headers=headers(**{"X-Mock-Agent-Result": "pending"}), json=PAYLOAD
    ).json()
    path = f"{BASE}/{created['id']}"
    pending = client.post(
        path + "/agent-activation",
        headers=headers(**{"X-Mock-Agent-Result": "pending"}),
        json={"expected_version": 1},
    )
    assert pending.status_code == 202
    result = client.post(
        path,
        headers=headers(),
        json={"expected_version": 2, "profile_status": "inactive"},
    )
    assert result.status_code == 200
    assert "profile_inactive" in result.json()["eligibility_reasons"]


def test_authorisation_and_provider_isolation(setup):
    client, _ = setup
    assert client.get(BASE).status_code == 401
    assert (
        client.post(BASE, headers=headers("dev-operator"), json=PAYLOAD).status_code
        == 403
    )
    resource = client.post(BASE, headers=headers(), json=PAYLOAD).json()
    other = headers("dev-other-provider")
    path = f"{BASE}/{resource['id']}"
    assert client.get(path, headers=other).status_code == 404
    assert (
        client.post(
            path, headers=other, json={"expected_version": 1, "name": "Attack"}
        ).status_code
        == 404
    )
    assert client.get(BASE, headers=other).json()["items"] == []
    # The same identifier is allowed for a different provider.
    assert client.post(BASE, headers=other, json=PAYLOAD).status_code == 201


@pytest.mark.parametrize(
    "details",
    [
        {"vehicle_category": "van", "max_weight_kg": -1, "powertrain": "electric"},
        {"storage_capacity_m3": 250},
        {**PAYLOAD["details"], "unsupported": True},
        {**PAYLOAD["details"], "timezone": "Not/AZone"},
        {**PAYLOAD["details"], "restrictions": [{"type": "anything"}]},
    ],
)
def test_type_specific_validation(setup, details):
    client, engine = setup
    result = client.post(BASE, headers=headers(), json={**PAYLOAD, "details": details})
    assert result.status_code == 422, result.text
    assert count(engine, resources) == 0


def test_replacement_details_and_server_controlled_fields(setup):
    client, engine = setup
    resource = client.post(BASE, headers=headers(), json=PAYLOAD).json()
    path = f"{BASE}/{resource['id']}"
    invalid = client.post(
        path,
        headers=headers(),
        json={"expected_version": 1, "details": {"max_weight_kg": 900}},
    )
    assert invalid.status_code == 422
    assert (
        client.post(
            path,
            headers=headers(),
            json={"expected_version": 1, "agent_status": "active"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, headers=headers(), json={"expected_version": 1, "name": None}
        ).status_code
        == 422
    )
    assert client.get(path, headers=headers()).json()["version"] == 1
    assert count(engine, receipts) == 1


@pytest.mark.parametrize(
    "kind,details",
    [
        (
            "depot",
            {
                "storage_capacity_m3": 250,
                "loading_bays": 2,
                "supported_vehicle_categories": ["van"],
            },
        ),
        (
            "warehouse",
            {"storage_capacity_m3": 1000, "handling_capabilities": ["ambient"]},
        ),
        (
            "locker",
            {
                "public_access": True,
                "max_parcel_weight_kg": 20,
                "compartments": [
                    {
                        "size": "small",
                        "count": 12,
                        "length_cm": 40,
                        "width_cm": 30,
                        "height_cm": 20,
                    }
                ],
            },
        ),
    ],
)
def test_all_resource_types(setup, kind, details):
    client, _ = setup
    payload = deepcopy(PAYLOAD)
    payload.update(resource_type=kind, details=details)
    response = client.post(BASE, headers=headers(), json=payload)
    assert response.status_code == 201, response.text


def test_filters_pagination_and_openapi(setup):
    client, _ = setup
    for index in range(3):
        assert (
            client.post(
                BASE,
                headers=headers(),
                json={**PAYLOAD, "external_identifier": f"VAN-{index}"},
            ).status_code
            == 201
        )
    first = client.get(
        BASE + "?limit=2&resource_type=vehicle", headers=headers()
    ).json()
    second = client.get(
        BASE + f"?limit=2&cursor={first['next_cursor']}", headers=headers()
    ).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert client.get(BASE + "?limit=101", headers=headers()).status_code == 422
    assert client.get("/").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths[BASE + "/{resource_id}"]
    assert "patch" not in paths[BASE + "/{resource_id}"]
    assert BASE + "/{resource_id}/agent-activation" in paths
