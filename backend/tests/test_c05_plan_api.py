"""C01 reads a proposal through the C05 route and C02 provider/run checks."""

from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.components.c02_identity_access import records
from app.components.c05_planning import confirmation
from app.components.c05_planning.store import PostgresPlanStore
from app.config import Settings
from app.main import create_app


@pytest.fixture
def api(example, monkeypatch):
    plan = example("plan-proposal.json")
    run_id = UUID(plan["runScope"]["runId"])
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    ).execution_options(schema_translate_map={"c02_identity_access": None})
    records.metadata.create_all(engine)
    provider, operator, driver = uuid4(), uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            records.providers.insert(),
            {"provider_id": provider, "provider_name": "Dublin"},
        )
        for actor, role in [(operator, "operator"), (driver, "driver")]:
            conn.execute(
                records.profiles.insert(),
                {"principal_id": actor, "display_name": role, "platform_admin": False},
            )
            conn.execute(
                records.memberships.insert(),
                {"principal_id": actor, "provider_id": provider, "role": role},
            )

    class Verifier:
        def subject(self, token):
            return UUID(token)

    # Use the actual planning service and router; PG persistence is tested separately.
    reads = []

    def lookup(store, scope, selected_run, plan_id, *, acting_provider_id):
        assert acting_provider_id == provider
        reads.append((scope, selected_run, plan_id))
        if (
            scope == "simulation"
            and selected_run == run_id
            and str(plan_id) == plan["planId"]
        ):
            return plan
        return None

    monkeypatch.setattr(PostgresPlanStore, "get_plan", lookup)
    settings = Settings(database_url="sqlite://", SHARED_RUN_IDS={run_id})
    try:
        with TestClient(
            create_app(engine, settings, identity_verifier=Verifier())
        ) as client:
            yield client, plan, operator, driver, reads
    finally:
        engine.dispose()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    envelope = response.json()
    assert set(envelope) == {"error"}
    assert set(envelope["error"]) == {"code", "message", "correlationId"}
    assert envelope["error"]["code"] == code
    assert envelope["error"]["message"]
    assert envelope["error"]["correlationId"] == response.headers["X-Correlation-ID"]


def test_operator_reads_proposal_only_in_its_partition(api):
    client, plan, operator, _, reads = api
    url = f"/api/v1/plans/{plan['planId']}"
    cid = str(uuid4())
    headers = {"Authorization": f"Bearer {operator}", "X-Correlation-ID": cid}
    response = client.get(url, params=plan["runScope"], headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == plan
    assert response.headers["X-Correlation-ID"] == cid
    assert_error(
        client.get(url, params={"scope": "live"}, headers=headers), 404, "NotFound"
    )
    assert reads == [
        ("simulation", UUID(plan["runScope"]["runId"]), UUID(plan["planId"])),
        ("live", None, UUID(plan["planId"])),
    ]


def test_sign_in_provider_role_and_shared_run_are_checked_before_the_read(api):
    client, plan, operator, driver, reads = api
    url = f"/api/v1/plans/{plan['planId']}"
    assert_error(client.get(url, params=plan["runScope"]), 401, "Unauthenticated")
    driver_auth = {"Authorization": f"Bearer {driver}"}
    assert_error(
        client.get(url, params=plan["runScope"], headers=driver_auth), 403, "Forbidden"
    )
    operator_auth = {"Authorization": f"Bearer {operator}"}
    other_run = {"scope": "simulation", "runId": str(uuid4())}
    assert_error(
        client.get(url, params=other_run, headers=operator_auth), 403, "Forbidden"
    )
    other_provider = {**operator_auth, "X-Acting-Provider": str(uuid4())}
    assert_error(
        client.get(url, params=plan["runScope"], headers=other_provider),
        403,
        "Forbidden",
    )
    assert reads == []


@pytest.mark.parametrize(
    "plan_id,params",
    [
        ("bad-id", {"scope": "live"}),
        (str(uuid4()), {}),
        (str(uuid4()), {"scope": "simulation"}),
        (str(uuid4()), {"scope": "live", "runId": str(uuid4())}),
        (str(uuid4()), {"scope": "simulation", "runId": "bad-id"}),
    ],
)
def test_malformed_plan_or_scope_uses_the_contract_error(api, plan_id, params):
    client, _, operator, _, reads = api
    response = client.get(
        f"/api/v1/plans/{plan_id}",
        params=params,
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert_error(response, 400, "Invalid")
    assert reads == []


def test_unavailable_plan_store_uses_the_contract_error(api, monkeypatch):
    client, plan, operator, _, _ = api

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("private database details")

    monkeypatch.setattr(PostgresPlanStore, "get_plan", unavailable)
    response = client.get(
        f"/api/v1/plans/{plan['planId']}",
        params=plan["runScope"],
        headers={"Authorization": f"Bearer {operator}"},
    )
    assert_error(response, 503, "Unavailable")
    assert "private database details" not in response.text


def test_confirmation_requires_operator_role_valid_version_and_idempotency_key(
    api, monkeypatch
):
    client, plan, operator, driver, _ = api
    attempts = []

    def capture_rejection(store, decisions, clock, context, plan_id, failure):
        attempts.append((context.principal_id, failure.kind))
        return failure

    monkeypatch.setattr(confirmation, "reject_confirmation", capture_rejection)
    path = f"/api/v1/plans/{plan['planId']}/confirmation"
    body = {"expectedVersion": 1, "runScope": plan["runScope"]}
    auth = {"Authorization": f"Bearer {operator}"}
    key = {"Idempotency-Key": "confirm-click"}
    assert_error(client.post(path, json=body, headers=key), 401, "Unauthenticated")
    assert_error(
        client.post(
            path, json=body, headers={**auth, **key, "X-Acting-Provider": str(uuid4())}
        ),
        403,
        "Forbidden",
    )
    assert attempts == []
    assert_error(
        client.post(
            path, json=body, headers={**key, "Authorization": f"Bearer {driver}"}
        ),
        403,
        "Forbidden",
    )
    assert_error(client.post(path, json=body, headers=auth), 400, "Invalid")
    assert_error(
        client.post(
            path, json={**body, "expectedVersion": True}, headers={**auth, **key}
        ),
        400,
        "Invalid",
    )
    assert attempts == [
        (driver, "Forbidden"),
        (operator, "Invalid"),
        (operator, "Invalid"),
    ]
