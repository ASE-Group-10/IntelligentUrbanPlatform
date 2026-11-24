"""C04 Delivery Management HTTP API, internal interfaces, outbox, and handoff tests."""

import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.components.c02_identity_access import records
from app.components.c02_identity_access.tokens import SupabaseVerifier
from app.components.c04_delivery_management import store as c04_store
from app.components.c04_delivery_management.contracts import (
    Delivery,
    DeliveryEvent,
    DeliveryProgress,
)
from app.components.c04_delivery_management.planning import PlanSummary
from app.components.c04_delivery_management.service import DeliveryService
from app.main import create_app
from app.shared.auth import AuthContext
from app.shared.contracts import RunScopeSelector
from app.shared.result import Failure, Ok

BASE = "/api/v1/deliveries"
DEMO_RUN = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "contracts/components/c04-delivery-management/openapi.yaml"
)

PROJECT = "https://c04-test.example.test"
SIGNER = ec.generate_private_key(ec.SECP256R1())
PROVIDER_A, PROVIDER_B = uuid4(), uuid4()
PRINCIPALS = {
    "operator": uuid4(),
    "manager": uuid4(),
    "driver": uuid4(),
    "other-provider": uuid4(),
}


def access_token(name: str = "operator") -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(PRINCIPALS[name]),
            "role": "authenticated",
            "iat": now,
            "iss": PROJECT + "/auth/v1",
            "aud": "authenticated",
            "exp": now + timedelta(minutes=5),
        },
        SIGNER,
        algorithm="ES256",
        headers={"kid": "c04-test-key"},
    )


def auth_headers(name: str = "operator", **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token(name)}", **extra}


def service_context(
    service_id: str = "C05", correlation_id: UUID | None = None
) -> AuthContext:
    return AuthContext(
        principal_id=UUID("00000000-0000-4000-8000-000000000005"),
        principal_type="service",
        service_id=service_id,
        acting_provider_id=None,
        roles=("service",),
        provider_ids=(),
        platform_roles=(),
        correlation_id=correlation_id or uuid4(),
    )


class RecordingPlanningGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Delivery, RunScopeSelector, UUID, str]] = []
        self.plan_id = uuid4()

    def generate_plan(self, delivery, run_scope, correlation_id, key):
        self.calls.append((delivery, run_scope, correlation_id, key))
        if self.fail:
            return Failure("Unavailable", "C05.UNAVAILABLE")
        return Ok(
            PlanSummary(
                plan_id=self.plan_id,
                version=1,
                status="awaiting_confirmation",
                stub=False,
            )
        )


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    ).execution_options(
        schema_translate_map={
            "c02_identity_access": None,
            "c04_delivery_management": None,
        }
    )
    records.metadata.create_all(engine)
    c04_store.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            records.providers.insert(),
            [
                {"provider_id": PROVIDER_A, "provider_name": "Dublin Fleet A"},
                {"provider_id": PROVIDER_B, "provider_name": "Dublin Fleet B"},
            ],
        )
        roles_map = {
            "operator": (PROVIDER_A, "operator"),
            "manager": (PROVIDER_A, "manager"),
            "driver": (PROVIDER_A, "driver"),
            "other-provider": (PROVIDER_B, "operator"),
        }
        for name, pid in PRINCIPALS.items():
            provider_id, role = roles_map[name]
            conn.execute(
                records.profiles.insert().values(
                    principal_id=pid, display_name=name, platform_admin=False
                )
            )
            conn.execute(
                records.memberships.insert().values(
                    principal_id=pid, provider_id=provider_id, role=role
                )
            )
    return engine


@pytest.fixture
def env(monkeypatch):
    engine = _make_engine()
    verifier = SupabaseVerifier(PROJECT)
    public_key = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(SIGNER.public_key()))
    monkeypatch.setattr(
        verifier.keys,
        "fetch_data",
        lambda: {
            "keys": [
                {**public_key, "kid": "c04-test-key", "alg": "ES256", "use": "sig"}
            ]
        },
    )
    settings = SimpleNamespace(
        database_url=SecretStr("sqlite://"),
        mock_dependencies=True,
        supabase_url=PROJECT,
        availability_max_age_seconds=86400,
        shared_run_ids=frozenset({DEMO_RUN}),
    )
    planner = RecordingPlanningGateway()
    fixed_now = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
    app = create_app(
        engine,
        settings,
        identity_verifier=verifier,
        city_state_store=object(),
        disruption_store=object(),
        planning_gateway=planner,
        clock=lambda: fixed_now,
    )
    with TestClient(app) as client:
        yield client, engine, app.state.deliveries, planner
    engine.dispose()


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.scalar(select(func.count()).select_from(table))


def test_submit_delivery_persists_emits_outbox_logs_trace_and_calls_c05(
    env, example, caplog
):
    client, engine, _, planner = env
    payload = example("submit-delivery-request.json")
    cid = str(uuid4())

    with caplog.at_level(logging.INFO, logger="ulop.c04"):
        response = client.post(
            BASE,
            params={"runId": str(DEMO_RUN)},
            headers=auth_headers("operator", **{"X-Correlation-ID": cid}),
            json=payload,
        )

    assert response.status_code == 201, response.text
    body = response.json()
    delivery = Delivery.model_validate(body)
    assert delivery.version == 1
    assert delivery.status == "pending"
    assert delivery.progress.status == "pending"
    assert delivery.planned_at is None
    assert response.headers["location"] == f"{BASE}/{delivery.delivery_id}"
    assert response.headers["idempotency-replayed"] == "false"
    assert response.headers["x-correlation-id"] == cid
    assert response.headers["x-plan-id"] == str(planner.plan_id)
    assert response.headers["x-plan-version"] == "1"

    # C05 in-process handoff received saved delivery, runScope and original trace.
    assert len(planner.calls) == 1
    handed_delivery, handed_scope, handed_cid, _ = planner.calls[0]
    assert handed_delivery.delivery_id == delivery.delivery_id
    assert handed_scope == RunScopeSelector(scope="simulation", run_id=DEMO_RUN)
    assert str(handed_cid) == cid

    # Outbox event validates strictly against C04 events.yaml contract.
    with engine.connect() as conn:
        outbox_rows = conn.execute(select(c04_store.outbox)).mappings().all()
    assert len(outbox_rows) == 1
    event = DeliveryEvent.model_validate(outbox_rows[0]["envelope"])
    assert event.event_type == "DeliverySubmitted"
    assert event.producer == "C04"
    assert str(event.correlation_id) == cid
    assert event.scope == "simulation"
    assert event.run_id == DEMO_RUN
    assert event.entity_versions == {str(delivery.delivery_id): 1}
    assert event.payload.delivery_id == delivery.delivery_id

    # Structured log record carries correlationId, component, scope, runId.
    c04_records = [r for r in caplog.records if getattr(r, "component", None) == "C04"]
    assert any(
        getattr(r, "correlationId", None) == cid
        and getattr(r, "scope", None) == "simulation"
        and getattr(r, "runId", None) == str(DEMO_RUN)
        for r in c04_records
    )

    # Audit outcome recorded atomically.
    assert count(engine, c04_store.audit_outcomes) == 1


@pytest.mark.parametrize("legacy_receipt", [False, True], ids=["current", "legacy"])
def test_submit_idempotent_replay_and_conflict(env, example, legacy_receipt):
    client, engine, _, planner = env
    payload = example("submit-delivery-request.json")
    original_trace = uuid4()

    first = client.post(
        BASE,
        headers=auth_headers("operator", **{"X-Correlation-ID": str(original_trace)}),
        json=payload,
    )
    assert first.status_code == 201

    if legacy_receipt:
        with engine.begin() as conn:
            conn.execute(
                c04_store.command_receipts.update().values(response_body=first.json())
            )
    replay = client.post(
        BASE,
        headers=auth_headers("operator", **{"X-Correlation-ID": str(uuid4())}),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first.json()
    assert replay.headers["x-correlation-id"] == str(original_trace)
    assert [call[2] for call in planner.calls] == [original_trace, original_trace]
    assert planner.calls[0][3] == planner.calls[1][3]
    assert count(engine, c04_store.deliveries) == 1
    assert count(engine, c04_store.outbox) == 1

    altered = deepcopy(payload)
    altered["cargo"]["weightKg"] = 9.0
    conflict = client.post(BASE, headers=auth_headers("operator"), json=altered)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "C04.IDEMPOTENCY_CONFLICT"
    assert count(engine, c04_store.deliveries) == 1


def test_submit_replay_cannot_return_a_delivery_from_another_acting_provider(
    env, example
):
    client, engine, _, planner = env
    payload = example("submit-delivery-request.json")
    with engine.begin() as conn:
        conn.execute(
            records.memberships.insert().values(
                principal_id=PRINCIPALS["operator"],
                provider_id=PROVIDER_B,
                role="operator",
            )
        )

    def submit_as(provider):
        return client.post(
            BASE,
            headers=auth_headers("operator", **{"X-Acting-Provider": str(provider)}),
            json=payload,
        )

    first = submit_as(PROVIDER_A)
    assert first.status_code == 201
    other = submit_as(PROVIDER_B)
    assert other.status_code == 409 and "x-plan-id" not in other.headers
    assert first.json()["deliveryId"] not in other.text
    assert len(planner.calls) == 1


def test_submit_location_and_validation_failures_store_nothing(env, example):
    client, engine, _, _ = env
    payload = example("submit-delivery-request.json")

    unmatched = client.post(
        BASE,
        headers=auth_headers("operator", **{"X-Mock-Location-Result": "unmatched"}),
        json=payload,
    )
    assert unmatched.status_code == 422
    assert unmatched.json()["error"]["code"] == "C04.LOCATION_UNMATCHED"

    unavailable = client.post(
        BASE,
        headers=auth_headers("operator", **{"X-Mock-Location-Result": "unconfirmed"}),
        json=payload,
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "C04.LOCATION_UNCONFIRMED"

    same_spot = deepcopy(payload)
    same_spot["destination"] = deepcopy(same_spot["pickup"])
    identical = client.post(BASE, headers=auth_headers("operator"), json=same_spot)
    assert identical.status_code == 422
    assert identical.json()["error"]["code"] == "C04.IDENTICAL_LOCATIONS"

    assert count(engine, c04_store.deliveries) == 0
    assert count(engine, c04_store.outbox) == 0


def test_authentication_role_provider_and_run_scope_isolation(env, example):
    client, _, _, _ = env
    payload = example("submit-delivery-request.json")

    assert client.post(BASE, json=payload).status_code == 401
    assert (
        client.post(BASE, headers=auth_headers("driver"), json=payload).status_code
        == 403
    )

    # Non-shared simulation run is rejected.
    unshared_run = str(uuid4())
    assert (
        client.post(
            BASE,
            params={"runId": unshared_run},
            headers=auth_headers("operator"),
            json=payload,
        ).status_code
        == 403
    )

    # Create in live scope for Provider A.
    created = client.post(BASE, headers=auth_headers("operator"), json=payload)
    assert created.status_code == 201
    delivery_id = created.json()["deliveryId"]

    # Another provider cannot read or list it.
    assert (
        client.get(
            f"{BASE}/{delivery_id}", headers=auth_headers("other-provider")
        ).status_code
        == 404
    )
    other_list = client.get(BASE, headers=auth_headers("other-provider"))
    assert other_list.status_code == 200
    assert other_list.json() == {"items": [], "nextCursor": None}

    # Same provider cannot see live delivery in simulation scope and may reuse key.
    assert (
        client.get(
            f"{BASE}/{delivery_id}",
            params={"runId": str(DEMO_RUN)},
            headers=auth_headers("operator"),
        ).status_code
        == 404
    )
    sim_created = client.post(
        BASE,
        params={"runId": str(DEMO_RUN)},
        headers=auth_headers("operator"),
        json=payload,
    )
    assert sim_created.status_code == 201
    assert sim_created.json()["deliveryId"] != delivery_id


def test_list_pagination_status_filter_and_update_lifecycle(env, example):
    client, _, _, _ = env
    payload_1 = example("submit-delivery-request.json")
    payload_2 = deepcopy(payload_1) | {"idempotencyKey": "delivery-request-002"}

    first = client.post(BASE, headers=auth_headers("operator"), json=payload_1).json()
    second = client.post(BASE, headers=auth_headers("operator"), json=payload_2).json()

    # Paginate with limit=1
    page_1 = client.get(
        BASE, params={"limit": 1}, headers=auth_headers("operator")
    ).json()
    assert len(page_1["items"]) == 1
    assert page_1["nextCursor"] is not None

    page_2 = client.get(
        BASE,
        params={"limit": 1, "cursor": page_1["nextCursor"]},
        headers=auth_headers("operator"),
    ).json()
    assert len(page_2["items"]) == 1
    assert page_2["nextCursor"] is None
    assert {page_1["items"][0]["deliveryId"], page_2["items"][0]["deliveryId"]} == {
        first["deliveryId"],
        second["deliveryId"],
    }

    # Update pending delivery
    update_body = deepcopy(payload_1)
    update_body["cargo"]["description"] = "Updated medical supplies"
    update_body["expectedVersion"] = 1
    update_body["idempotencyKey"] = "update-001"
    updated = client.post(
        f"{BASE}/{first['deliveryId']}",
        headers=auth_headers("manager"),
        json=update_body,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["cargo"]["description"] == "Updated medical supplies"

    # Idempotent update replay
    replay = client.post(
        f"{BASE}/{first['deliveryId']}",
        headers=auth_headers("manager"),
        json=update_body,
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"

    # Stale expectedVersion returns 409
    stale = deepcopy(update_body) | {"idempotencyKey": "update-002"}
    conflict = client.post(
        f"{BASE}/{first['deliveryId']}",
        headers=auth_headers("manager"),
        json=stale,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "C04.VERSION_CONFLICT"


def test_planning_failure_still_persists_delivery(env, example):
    client, engine, service, _ = env
    service.planning_gateway = RecordingPlanningGateway(fail=True)
    response = client.post(
        BASE,
        headers=auth_headers("operator"),
        json=example("submit-delivery-request.json"),
    )
    assert response.status_code == 201
    assert "x-plan-id" not in response.headers
    assert count(engine, c04_store.deliveries) == 1


def test_internal_get_requests_mark_planned_and_update_progress(env, example):
    client, engine, service, _ = env
    assert isinstance(service, DeliveryService)
    sim_scope = RunScopeSelector(scope="simulation", run_id=DEMO_RUN)
    live_scope = RunScopeSelector(scope="live")

    p1 = example("submit-delivery-request.json")
    p2 = deepcopy(p1) | {"idempotencyKey": "delivery-request-002"}
    d1 = UUID(
        client.post(
            BASE,
            params={"runId": str(DEMO_RUN)},
            headers=auth_headers("operator"),
            json=p1,
        ).json()["deliveryId"]
    )
    d2 = UUID(
        client.post(
            BASE,
            params={"runId": str(DEMO_RUN)},
            headers=auth_headers("operator"),
            json=p2,
        ).json()["deliveryId"]
    )

    c05_ctx = service_context("C05")
    # Cross-scope lookup rejects with NotFound
    assert service.get_requests(c05_ctx, live_scope, [d1]) == Failure(
        "NotFound", "C04.DELIVERY_NOT_FOUND"
    )
    fetched = service.get_requests(c05_ctx, sim_scope, [d1, d2])
    assert isinstance(fetched, Ok)
    assert [d.delivery_id for d in fetched.value] == [d1, d2]

    # Non-C05 caller cannot markRequestsPlanned
    plan_id = uuid4()
    assert service.mark_requests_planned(
        service_context("C07"), sim_scope, [d1], plan_id, {d1: 1}, "link-bad"
    ) == Failure("Forbidden", "C04.FORBIDDEN")

    # Cross-run / missing ID commits no partial links
    missing_id = uuid4()
    assert service.mark_requests_planned(
        c05_ctx,
        sim_scope,
        [d1, missing_id],
        plan_id,
        {d1: 1, missing_id: 1},
        "link-miss",
    ) == Failure("NotFound", "C04.DELIVERY_NOT_FOUND")
    assert service.get_requests(c05_ctx, sim_scope, [d1]).value[0].status == "pending"

    # Stale expectedVersion in multi-request call commits no partial links
    assert service.mark_requests_planned(
        c05_ctx, sim_scope, [d1, d2], plan_id, {d1: 1, d2: 99}, "link-stale"
    ) == Failure("Conflict", "C04.VERSION_CONFLICT")
    assert service.get_requests(c05_ctx, sim_scope, [d1]).value[0].status == "pending"

    # Valid link succeeds and bumps both versions
    linked = service.mark_requests_planned(
        c05_ctx, sim_scope, [d1, d2], plan_id, {d1: 1, d2: 1}, "link-ok"
    )
    assert isinstance(linked, Ok)
    assert linked.value.versions == {d1: 2, d2: 2}
    assert linked.value.plan_id == plan_id

    # Idempotent replay returns same receipt
    replayed = service.mark_requests_planned(
        c05_ctx, sim_scope, [d1, d2], plan_id, {d1: 1, d2: 1}, "link-ok"
    )
    assert replayed == linked

    # Planned delivery can no longer be updated via HTTP update endpoint
    update_attempt = deepcopy(p1) | {
        "expectedVersion": 2,
        "idempotencyKey": "after-plan",
    }
    rejected = client.post(
        f"{BASE}/{d1}",
        params={"runId": str(DEMO_RUN)},
        headers=auth_headers("operator"),
        json=update_attempt,
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "C04.DELIVERY_NOT_PENDING"

    # Progress update keeps business time and emits DeliveryProgressChanged.
    business_time = datetime(2026, 11, 20, 10, 42, 0, tzinfo=UTC)
    prog = DeliveryProgress(
        status="delivered",
        updated_at=business_time,
        note="Signed at Trinity College Dublin",
    )
    adapter_ctx = service_context("C08")
    progressed = service.update_progress(adapter_ctx, sim_scope, d1, prog, "prog-1")
    assert isinstance(progressed, Ok)
    assert progressed.value.version == 3
    assert progressed.value.status == "delivered"
    assert progressed.value.progress.updated_at == business_time

    # Terminal transition is rejected
    invalid_next = service.update_progress(
        adapter_ctx,
        sim_scope,
        d1,
        DeliveryProgress(status="in_transit", updated_at=business_time),
        "prog-2",
    )
    assert invalid_next == Failure("Invalid", "C04.INVALID_PROGRESS_TRANSITION")

    # Outbox DeliveryProgressChanged carries planId and observedAt = business_time.
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(c04_store.outbox).where(
                    c04_store.outbox.c.event_type == "DeliveryProgressChanged"
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    ev = DeliveryEvent.model_validate(rows[0]["envelope"])
    assert ev.observed_at == business_time
    assert ev.affected.plan_ids == [plan_id]
    assert ev.payload.delivery.progress.updated_at == business_time


def test_generated_openapi_matches_c04_contract_operations(env):
    client, _, _, _ = env
    expected = yaml.safe_load(CONTRACT.read_text())["paths"]
    generated = client.get("/openapi.json").json()["paths"]

    for path, methods in expected.items():
        assert path in generated
        for method, spec in methods.items():
            assert method in generated[path]
            assert generated[path][method]["operationId"] == spec["operationId"]
