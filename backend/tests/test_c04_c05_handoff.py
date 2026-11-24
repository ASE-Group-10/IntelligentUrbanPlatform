"""C04 submission, C05 generation and confirmation through the real HTTP app."""

from uuid import UUID, uuid4

import psycopg
import pytest

from app.demo.c05_confirmation import confirm_demo_plan
from app.demo.c05_evidence import collect_evidence
from app.shared.result import Failure
from tests.c05_flow import flow as flow
from tests.test_c05_demo_confirmation import demo_dsn as demo_dsn


@pytest.mark.parametrize(
    "interrupted", [False, True], ids=["first-submit", "retry-after-outage"]
)
def test_delivery_submission_hands_off_one_traced_proposal_to_plans(
    flow, example, test_database_url, demo_dsn, monkeypatch, interrupted
):
    client = flow.client
    run_id = str(flow.plan.run_scope.run_id)
    trace = str(uuid4())
    body = example("submit-delivery-request.json")
    url = "/api/v1/deliveries"
    headers = {**flow.headers, "X-Correlation-ID": trace}
    params = {"runId": run_id}
    if interrupted:
        with monkeypatch.context() as m:
            m.setattr(
                client.app.state.knowledge,
                "query_knowledge",
                lambda *args, **kwargs: Failure("Unavailable", "C09.UNAVAILABLE"),
            )
            pending = client.post(url, params=params, headers=headers, json=body)
        assert pending.status_code == 201 and "x-plan-id" not in pending.headers
        headers["X-Correlation-ID"] = str(uuid4())

    saved = client.post(url, params=params, headers=headers, json=body)
    assert saved.status_code == (200 if interrupted else 201), saved.text
    assert saved.headers["x-correlation-id"] == trace
    assert saved.headers["x-plan-version"] == "1" and "x-plan-stub" not in saved.headers
    plan_id = saved.headers["x-plan-id"]
    delivery_id = saved.json()["deliveryId"]
    plan_url = f"/api/v1/plans/{plan_id}"
    selected = {"scope": "simulation", "runId": run_id}
    proposal = client.get(plan_url, params=selected, headers=flow.headers)
    assert proposal.status_code == 200, proposal.text
    assert proposal.json()["requestIds"] == [delivery_id]
    assert proposal.json()["status"] == "awaiting_confirmation"
    assert confirm_demo_plan(
        demo_dsn,
        flow.plan.run_scope,
        frozenset({UUID(run_id)}),
        UUID(plan_id),
        UUID(trace),
    ) == Failure("NotFound", "No imported demo proposal in this run.")

    with psycopg.connect(test_database_url) as conn:
        report = collect_evidence(
            conn, run_id=UUID(run_id), correlation_id=UUID(trace), plan_id=UUID(plan_id)
        )
        assert report["origin"] == "c04-submission" and report["generation"]["ok"], (
            report
        )
        assert conn.execute(
            "select correlation_id from c05_planning.plan_versions where plan_id=%s",
            (plan_id,),
        ).fetchall() == [(UUID(trace),)]
        assert conn.execute(
            "select count(*) from c05_planning.outbox where plan_id=%s", (plan_id,)
        ).fetchone() == (0,)

    confirmed = client.post(
        plan_url + "/confirmation",
        headers=flow.headers,
        json={"expectedVersion": 1, "runScope": selected},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["version"] == 2
    replay = client.post(
        url,
        params=params,
        headers={**headers, "X-Correlation-ID": str(uuid4())},
        json=body,
    )
    assert replay.status_code == 200 and replay.json() == saved.json()
    assert replay.headers["x-correlation-id"] == trace
    assert (
        replay.headers["x-plan-id"] == plan_id
        and replay.headers["x-plan-version"] == "1"
    )
    with psycopg.connect(test_database_url) as conn:
        assert conn.execute(
            "select status, plan_id from c04_delivery_management.deliveries "
            "where delivery_id=%s and run_id=%s",
            (delivery_id, run_id),
        ).fetchone() == ("planned", UUID(plan_id))
        assert conn.execute(
            "select envelope->>'correlationId', envelope->>'runId' "
            "from c05_planning.outbox where plan_id=%s",
            (plan_id,),
        ).fetchall() == [(trace, run_id)]
