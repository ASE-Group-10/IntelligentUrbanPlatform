"""Signed-in C05 operations use the C04 owner of every submitted delivery."""

from uuid import uuid4

import psycopg
import pytest

from tests.c05_flow import flow as flow


@pytest.fixture
def second_provider(flow, test_database_url):
    provider = uuid4()
    with psycopg.connect(test_database_url) as conn:
        conn.execute(
            "insert into c02_identity_access.providers values (%s, 'second provider')",
            (provider,),
        )
        conn.execute(
            "insert into c02_identity_access.provider_memberships "
            "values (%s, %s, 'operator')",
            (flow.operator, provider),
        )
    return {**flow.headers, "X-Acting-Provider": str(provider)}


@pytest.fixture
def submitted(flow, example):
    scope = flow.plan.run_scope.model_dump(mode="json", by_alias=True)
    response = flow.client.post(
        "/api/v1/deliveries",
        headers=flow.headers,
        params={"runId": scope["runId"]},
        json=example("submit-delivery-request.json"),
    )
    assert response.status_code == 201, response.text
    return response.headers["x-plan-id"], response.json()["deliveryId"], scope


def test_operator_provider_owns_read_confirmation_and_receipt_replay(
    flow, submitted, second_provider, test_database_url
):
    plan_id, delivery_id, scope = submitted
    client = flow.client
    url = f"/api/v1/plans/{plan_id}"
    missing = f"/api/v1/plans/{uuid4()}"
    command = {"expectedVersion": 1, "runScope": scope}

    def assert_hidden(headers):
        read = client.get(url, headers=headers, params=scope)
        unknown = client.get(missing, headers=headers, params=scope)
        assert read.status_code == unknown.status_code == 404
        assert read.json() == unknown.json()
        deny = client.post(url + "/confirmation", headers=headers, json=command)
        unknown = client.post(missing + "/confirmation", headers=headers, json=command)
        assert deny.status_code == unknown.status_code == 404
        assert deny.json() == unknown.json()

    assert_hidden(second_provider)
    assert client.get(url, headers=flow.headers, params=scope).status_code == 200
    with psycopg.connect(test_database_url) as conn:
        assert conn.execute(
            "select version, plan_id from c04_delivery_management.deliveries "
            "where run_id=%s and delivery_id=%s",
            (scope["runId"], delivery_id),
        ).fetchone() == (1, None)
        assert conn.execute(
            "select count(*) from c05_planning.outbox where plan_id=%s", (plan_id,)
        ).fetchone() == (0,)
    confirmed = client.post(url + "/confirmation", headers=flow.headers, json=command)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["version"] == 2
    assert (
        client.post(url + "/confirmation", headers=flow.headers, json=command).json()
        == confirmed.json()
    )

    # Simulate changed authoritative ownership after a receipt was committed.
    with psycopg.connect(test_database_url) as conn:
        conn.execute(
            "update c04_delivery_management.deliveries set provider_id=%s "
            "where run_id=%s and delivery_id=%s",
            (second_provider["X-Acting-Provider"], scope["runId"], delivery_id),
        )
    assert_hidden(flow.headers)
    assert (
        client.get(url, params=scope, headers=second_provider).json()
        == confirmed.json()
    )
    with psycopg.connect(test_database_url) as conn:
        assert conn.execute(
            "select count(*) from c05_planning.outbox where plan_id=%s", (plan_id,)
        ).fetchone() == (1,)


@pytest.mark.parametrize("extra", ["absent", "shared-import", "other-provider"])
def test_provider_plan_with_missing_or_mixed_authority_is_hidden(
    flow, submitted, second_provider, test_database_url, example, extra
):
    source_id, _, scope = submitted
    # The original fixture is an explicitly imported snapshot shared within its run.
    shared_url = f"/api/v1/plans/{flow.plan.plan_id}"
    assert (
        flow.client.get(shared_url, headers=second_provider, params=scope).status_code
        == 200
    )
    extra_id = uuid4()
    if extra == "shared-import":
        extra_id = flow.plan.request_ids[0]
    elif extra == "other-provider":
        response = flow.client.post(
            "/api/v1/deliveries",
            headers=second_provider,
            params={"runId": scope["runId"]},
            json={
                **example("submit-delivery-request.json"),
                "idempotencyKey": "second-delivery",
            },
        )
        assert response.status_code == 201, response.text
        extra_id = response.json()["deliveryId"]
    plan_id = uuid4()
    with psycopg.connect(test_database_url) as conn:
        conn.execute(
            """insert into c05_planning.plan_versions
                      (proposal, correlation_id, planned_departure_at)
               select proposal || jsonb_build_object(
                          'planId', %s::text,
                          'requestIds', (proposal->'requestIds') || to_jsonb(%s::text)),
                      correlation_id, planned_departure_at
                 from c05_planning.plan_versions
                where scope_key=%s and plan_id=%s and version=1""",
            (str(plan_id), str(extra_id), f"simulation:{scope['runId']}", source_id),
        )
    url = f"/api/v1/plans/{plan_id}"
    for headers in (flow.headers, second_provider):
        assert flow.client.get(url, params=scope, headers=headers).status_code == 404
        confirmation = flow.client.post(
            url + "/confirmation",
            headers=headers,
            json={"expectedVersion": 1, "runScope": scope},
        )
        assert confirmation.status_code == 404, confirmation.text
    with psycopg.connect(test_database_url) as conn:
        assert conn.execute(
            "select count(*) from c05_planning.outbox where plan_id=%s", (plan_id,)
        ).fetchone() == (0,)
