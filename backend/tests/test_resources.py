import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.components.c02_identity_access import records
from app.components.c02_identity_access.tokens import SupabaseVerifier
from app.infrastructure.database import audit, metadata, outbox, receipts, resources
from app.main import create_app

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


PROJECT = "https://c02.example.test"
SIGNER = ec.generate_private_key(ec.SECP256R1())
PRINCIPALS = {
    name: uuid4() for name in ("dev-manager", "dev-operator", "dev-other-provider")
}
PROVIDER_A, PROVIDER_B = uuid4(), uuid4()


def access_token(name="dev-manager", **claims):
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(PRINCIPALS[name]),
            "role": "authenticated",
            "iat": now,
            "iss": PROJECT + "/auth/v1",
            "aud": "authenticated",
            "exp": now + timedelta(minutes=5),
            **claims,
        },
        SIGNER,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )


def headers(token="dev-manager", **extra):
    return {
        "Authorization": f"Bearer {access_token(token)}",
        "Idempotency-Key": str(uuid4()),
        **extra,
    }


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    engine = engine.execution_options(
        schema_translate_map={"c02_identity_access": None}
    )
    metadata.create_all(engine)
    records.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            records.providers.insert(),
            [
                {"provider_id": pid, "provider_name": name}
                for pid, name in [(PROVIDER_A, "A"), (PROVIDER_B, "B")]
            ],
        )
        for name, pid in PRINCIPALS.items():
            conn.execute(
                records.profiles.insert().values(
                    principal_id=pid, display_name=name, platform_admin=False
                )
            )
            conn.execute(
                records.memberships.insert().values(
                    principal_id=pid,
                    provider_id=PROVIDER_B
                    if name == "dev-other-provider"
                    else PROVIDER_A,
                    role="operator" if name == "dev-operator" else "manager",
                )
            )
    verifier = SupabaseVerifier(PROJECT)
    public_key = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(SIGNER.public_key()))
    monkeypatch.setattr(
        verifier.keys,
        "fetch_data",
        lambda: {
            "keys": [{**public_key, "kid": "test-key", "alg": "ES256", "use": "sig"}]
        },
    )

    settings = SimpleNamespace(
        database_url=SecretStr("sqlite://"),
        mock_dependencies=True,
        supabase_url=PROJECT,
        availability_max_age_seconds=86400,
    )
    with TestClient(create_app(engine, settings, identity_verifier=verifier)) as client:
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


def test_admin_selects_existing_provider_without_membership(setup):
    client, engine = setup
    admin = uuid4()
    with engine.begin() as conn:
        conn.execute(
            records.profiles.insert().values(
                principal_id=admin, display_name="Platform admin", platform_admin=True
            )
        )
    admin_headers = headers(Authorization="Bearer " + access_token(sub=str(admin)))
    profile = client.get("/api/v1/identity/me", headers=admin_headers).json()
    assert profile["memberships"] == []
    assert profile["platformRoles"] == ["admin"]
    assert {
        p["providerId"]: p["providerName"] for p in profile["availableProviders"]
    } == {
        str(PROVIDER_A): "A",
        str(PROVIDER_B): "B",
    }
    assert client.post(BASE, headers=admin_headers, json=PAYLOAD).status_code == 422
    missing = {**admin_headers, "X-Acting-Provider": str(uuid4())}
    assert client.post(BASE, headers=missing, json=PAYLOAD).status_code == 403

    manager = client.get("/api/v1/identity/me", headers=headers()).json()
    assert [p["providerId"] for p in manager["availableProviders"]] == [str(PROVIDER_A)]
    wrong_provider = headers(**{"X-Acting-Provider": str(PROVIDER_B)})
    assert client.post(BASE, headers=wrong_provider, json=PAYLOAD).status_code == 403
    for table in [resources, audit, receipts, outbox]:
        assert count(engine, table) == 0

    for pid in (PROVIDER_A, PROVIDER_B):
        selected = {
            **admin_headers,
            "X-Acting-Provider": str(pid),
            "Idempotency-Key": str(uuid4()),
        }
        assert client.post(BASE, headers=selected, json=PAYLOAD).status_code == 201
        assert len(client.get(BASE, headers=selected).json()["items"]) == 1
    with engine.connect() as conn:
        assert set(conn.execute(select(audit.c.principal_id, audit.c.provider_id))) == {
            (admin, PROVIDER_A),
            (admin, PROVIDER_B),
        }


def test_multi_provider_registration_and_recorded_actor(setup):
    client, engine = setup
    actor = PRINCIPALS["dev-manager"]
    with engine.begin() as conn:
        conn.execute(
            records.memberships.insert().values(
                principal_id=actor, provider_id=PROVIDER_B, role="operator"
            )
        )
    me = client.get("/api/v1/identity/me", headers=headers())
    assert me.status_code == 200
    profile = me.json()
    assert profile["principalId"] == str(actor)
    assert {m["providerId"]: m["roles"] for m in profile["memberships"]} == {
        str(PROVIDER_A): ["manager"],
        str(PROVIDER_B): ["operator"],
    }
    assert client.post(BASE, headers=headers(), json=PAYLOAD).status_code == 422
    for denied in [PROVIDER_B, uuid4()]:
        response = client.post(
            BASE, headers=headers(**{"X-Acting-Provider": str(denied)}), json=PAYLOAD
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
    for table in [resources, receipts, audit, outbox]:
        assert count(engine, table) == 0
    response = client.post(
        BASE, headers=headers(**{"X-Acting-Provider": str(PROVIDER_A)}), json=PAYLOAD
    )
    assert response.status_code == 201
    with engine.connect() as conn:
        assert conn.execute(
            select(audit.c.principal_id, audit.c.provider_id)
        ).one() == (actor, PROVIDER_A)


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 10},
        {"iss": "https://other.example.test/auth/v1"},
        {"aud": "other"},
    ],
)
def test_invalid_tokens_are_denied_before_registration(setup, claims):
    client, engine = setup
    request_headers = headers()
    request_headers["Authorization"] = "Bearer " + access_token(**claims)
    response = client.post(BASE, headers=request_headers, json=PAYLOAD)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert count(engine, resources) == count(engine, audit) == 0


def test_mock_token_no_longer_authenticates(setup):
    client, _ = setup
    assert (
        client.get(
            "/api/v1/identity/me", headers={"Authorization": "Bearer dev-manager"}
        ).status_code
        == 401
    )


def test_unprovisioned_user_can_read_empty_identity(setup):
    client, _ = setup
    request_headers = headers()
    request_headers["Authorization"] = "Bearer " + access_token(sub=str(uuid4()))
    assert (
        client.get("/api/v1/identity/me", headers=request_headers).json()["memberships"]
        == []
    )
    assert client.post(BASE, headers=request_headers, json=PAYLOAD).status_code == 403


def test_identity_provider_outage_fails_closed(setup, monkeypatch):
    from jwt.exceptions import PyJWKClientConnectionError

    client, engine = setup

    def unavailable():
        raise PyJWKClientConnectionError("Signing keys unavailable")

    monkeypatch.setattr(
        client.app.state.identity_verifier.keys, "fetch_data", unavailable
    )
    response = client.post(BASE, headers=headers(), json=PAYLOAD)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "IDENTITY_UNAVAILABLE"
    assert count(engine, resources) == count(engine, audit) == 0
