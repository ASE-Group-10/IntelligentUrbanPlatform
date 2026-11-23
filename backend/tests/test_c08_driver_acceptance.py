"""Repeatable hop 6: real broker, PostgreSQL, C02 records and FastAPI routes."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pika
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.components.c02_identity_access import records
from app.components.c06_city_monitoring.publisher import RabbitMQPublisher
from app.components.c08_allocation.store import AllocationStore
from app.components.c08_allocation.worker import make_consumer
from app.config import Settings
from app.demo.c08 import new_plan, save_demo_revision, seed_plan, shared_plan
from app.demo.feed import load_scenario
from app.infrastructure.database import make_engine
from app.main import create_app
from tests.test_resources import PAYLOAD


def headers(actor):
    return {"Authorization": f"Bearer {actor}", "Idempotency-Key": str(uuid4())}


@pytest.fixture
def scenario(test_database_url):
    engine = make_engine(
        test_database_url.replace("postgres://", "postgresql+psycopg://").replace(
            "postgresql://", "postgresql+psycopg://"
        )
    )
    store = AllocationStore(engine, 86400)
    provider, operator, driver, other = (uuid4() for _ in range(4))
    with engine.begin() as conn:
        conn.execute(
            records.providers.insert().values(
                provider_id=provider, provider_name="Hop 6"
            )
        )
        for actor, roles in [
            (operator, ["manager", "operator"]),
            (driver, ["driver"]),
            (other, ["driver"]),
        ]:
            conn.execute(
                records.profiles.insert().values(
                    principal_id=actor, display_name="Demo", platform_admin=False
                )
            )
            for role in roles:
                conn.execute(
                    records.memberships.insert().values(
                        principal_id=actor, provider_id=provider, role=role
                    )
                )

    # Only token verification is replaced; real C02 role and provider checks run.
    class Verifier:
        def subject(self, token):
            return UUID(token)

    app = create_app(
        engine, Settings(database_url=test_database_url), identity_verifier=Verifier()
    )
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/resources", headers=headers(operator), json=PAYLOAD
            )
            assert created.status_code == 201, created.text
            yield SimpleNamespace(
                client=client,
                store=store,
                resource=UUID(created.json()["id"]),
                provider=provider,
                operator=operator,
                driver=driver,
                other=other,
            )
    finally:
        engine.dispose()


@pytest.fixture
def deliver(scenario):
    broker = os.getenv("TEST_RABBITMQ_URL")
    if not broker:
        pytest.skip("needs a test RabbitMQ broker")
    store = scenario.store
    exchange, queue = f"c08.test.{uuid4()}", f"c08.test.{uuid4()}"
    publisher = RabbitMQPublisher(broker, exchange, {queue: ["PlanReady"]})

    def publish_and_consume(event):
        publisher.publish(event)
        consumer = make_consumer(store, broker, exchange, queue)
        try:
            consumer.declare()
            assert consumer.consume_available(max_messages=1) == 1
        finally:
            consumer.close()

    try:
        yield publish_and_consume
    finally:
        publisher.close()
        with pika.BlockingConnection(pika.URLParameters(broker)) as connection:
            channel = connection.channel()
            channel.queue_delete(queue)
            channel.exchange_delete(exchange)


@pytest.mark.parametrize(
    "plan_factory", [new_plan, shared_plan], ids=["live", "dublin"]
)
def test_plan_ready_to_driver_acceptance(scenario, plan_factory, deliver):
    client, store = scenario.client, scenario.store
    operator, driver, other = scenario.operator, scenario.driver, scenario.other
    event = plan_factory()
    query = f"?runId={event.run_id}" if event.run_id else ""
    live_path = "/api/v1/vehicle-assignments"
    path = live_path + query
    seed_plan(store, event, scenario.provider, scenario.resource, driver)
    seed_plan(store, event, scenario.provider, scenario.resource, driver)  # rerun demo
    old = json.loads(event.model_dump_json(by_alias=True))
    old["eventId"] = str(uuid4())
    old["payload"]["planVersion"] -= 1
    old["entityVersions"][str(event.payload.plan_id)] -= 1
    deliver(type(event).model_validate_json(json.dumps(old)))
    assert client.get(path, headers=headers(operator)).json() == []
    deliver(event)
    deliver(event)  # New consumer connection, same durable state.
    [allocated] = client.get(path, headers=headers(operator)).json()
    if event.run_id:
        assert client.get(live_path, headers=headers(operator)).json() == []
    assert allocated["status"] == "allocated" and allocated["canOffer"]
    assert client.get(path, headers=headers(driver)).json() == []
    command = {key: allocated[key] for key in ("planId", "deliveryId")}
    command["expectedVersion"] = allocated["planVersion"]
    offer_headers = headers(operator)
    offer = client.post(path, headers=offer_headers, json=command)
    assert offer.status_code == 200, offer.text
    assert offer.json()["assignmentRef"] == allocated["assignmentRef"]
    [pending] = client.get(path, headers=headers(driver)).json()
    live_respond = f"/api/v1/driver-offers/{pending['offerId']}/response"
    respond = live_respond + query
    assert pending["canRespond"]
    assert client.get(path, headers=headers(other)).json() == []
    assert (
        client.post(
            respond, headers=headers(other), json={"response": "accept"}
        ).status_code
        == 403
    )
    assert client.get(path, headers=headers(driver)).json()[0]["status"] == "offered"
    key = headers(driver)
    accepted = client.post(respond, headers=key, json={"response": "accept"})
    assert accepted.status_code == 200, accepted.text
    if event.run_id:
        # Even a previously successful command key cannot replay across scopes.
        assert (
            client.post(live_path, headers=offer_headers, json=command).status_code
            == 409
        )
        assert (
            client.post(
                live_respond, headers=key, json={"response": "accept"}
            ).status_code
            == 403
        )
    assert accepted.json()["code"] == "assigned"
    assert accepted.headers["X-Correlation-ID"] == str(event.correlation_id)
    assert (
        client.post(respond, headers=key, json={"response": "accept"}).json()
        == accepted.json()
    )
    assert (
        client.post(respond, headers=key, json={"response": "reject"}).status_code
        == 409
    )
    assert client.post(path, headers=offer_headers, json=command).json() == offer.json()
    for principal in (operator, driver):
        [saved] = client.get(path, headers=headers(principal)).json()
        assert saved["assignmentRef"] == allocated["assignmentRef"]
        assert saved["status"] == "accepted"
    with store.engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT count(*), sum(capacity_kg) "
                "FROM c08_allocation.allocations WHERE resource_id=:resource"
            ),
            {"resource": scenario.resource},
        ).one() == (1, event.payload.requirements[0].capacity.amount)


def test_competing_plans_cannot_overbook_one_vehicle(scenario):
    plans = [new_plan(), new_plan()]
    for event in plans:
        event.payload.requirements[0].capacity.amount = 500
        seed_plan(
            scenario.store, event, scenario.provider, scenario.resource, scenario.driver
        )
    barrier = Barrier(2)

    def commit(event):
        barrier.wait(timeout=5)
        return scenario.store.consume(event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(commit, plans)) == ["allocated", "insufficient capacity"]
    with scenario.store.engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT sum(capacity_kg) FROM c08_allocation.allocations "
                    "WHERE resource_id=:resource"
                ),
                {"resource": scenario.resource},
            ).scalar_one()
            == 500
        )


def test_capacity_and_plan_identity_are_isolated_per_run(scenario):
    store = scenario.store
    live = new_plan()
    live.payload.requirements[0].capacity.amount = 800  # entire test vehicle
    for run_id in (uuid4(), uuid4(), None):
        event = live.model_copy(
            update={
                "scope": "simulation" if run_id else "live",
                "run_id": run_id,
                "event_id": uuid4(),
            }
        )
        assert store.consume(event) == "outside configured demo"
        seed_plan(store, event, scenario.provider, scenario.resource, scenario.driver)
        assert store.consume(event) == "allocated"
        assert store.consume(event) == "duplicate"
        params = {"runId": str(run_id)} if run_id else {}
        rows = scenario.client.get(
            "/api/v1/vehicle-assignments",
            params=params,
            headers=headers(scenario.operator),
        ).json()
        assert len(rows) == 1
        assert rows[0]["correlationId"] == str(event.correlation_id)


@pytest.mark.parametrize("initial_status", ["allocated", "offered", "accepted"])
def test_route_only_revision_preserves_commitment(
    scenario, deliver, example, initial_status
):
    # Use a separate run so this test and the scripted Dublin demo can coexist.
    run_id = uuid4()
    initial = shared_plan().model_copy(update={"run_id": run_id, "event_id": uuid4()})
    wire = example("plan-ready-revision-event.json")
    wire.update(runId=str(run_id), eventId=str(uuid4()))
    revision = type(initial).model_validate_json(json.dumps(wire))
    revised_plan = example("revised-plan.json")
    store = scenario.store
    # The reservation fills this van. Rechecking v4 must exclude its own 10 kg.
    resized = scenario.client.post(
        f"/api/v1/resources/{scenario.resource}",
        headers=headers(scenario.operator),
        json={
            "expected_version": 1,
            "details": {**PAYLOAD["details"], "max_weight_kg": 10},
        },
    )
    assert resized.status_code == 200, resized.text
    other_run = uuid4()
    other = initial.model_copy(update={"run_id": other_run, "event_id": uuid4()})
    seed_plan(store, other, scenario.provider, scenario.resource, scenario.driver)
    deliver(other)
    seed_plan(store, initial, scenario.provider, scenario.resource, scenario.driver)
    deliver(initial)

    path = f"/api/v1/vehicle-assignments?runId={run_id}"

    def allocation():
        response = scenario.client.get(path, headers=headers(scenario.operator))
        assert response.status_code == 200, response.text
        [row] = response.json()
        return row

    original = allocation()
    assert original["planVersion"] == 3
    command = {
        "planId": original["planId"],
        "deliveryId": original["deliveryId"],
        "expectedVersion": 3,
    }

    def offer():
        return scenario.client.post(
            path, headers=headers(scenario.operator), json=command
        )

    if initial_status != "allocated":
        assert offer().status_code == 200
    if initial_status == "accepted":
        offer_id = allocation()["offerId"]
        response = scenario.client.post(
            f"/api/v1/driver-offers/{offer_id}/response?runId={run_id}",
            headers=headers(scenario.driver),
            json={"response": "accept"},
        )
        assert response.status_code == 200
    previous_offer = allocation()["offerId"]
    assert store.consume(revision) == "plan unavailable"
    revised_plan["runScope"]["runId"] = str(run_id)
    save_demo_revision(store, revised_plan, load_scenario().start_time)
    conflicting = revision.model_copy(deep=True, update={"event_id": uuid4()})
    conflicting.payload.decision_id = uuid4()
    assert store.consume(conflicting) == "event differs from saved plan"
    deliver(revision)
    deliver(revision)
    deliver(initial)
    deliver(initial.model_copy(update={"event_id": uuid4()}))
    untouched = scenario.client.get(
        f"/api/v1/vehicle-assignments?runId={other_run}",
        headers=headers(scenario.operator),
    ).json()
    assert [row["planVersion"] for row in untouched] == [3]
    current = allocation()
    assert current["planVersion"] == 4
    assert current["assignmentRef"] == original["assignmentRef"]
    assert current["status"] == initial_status
    assert current["offerId"] == previous_offer
    assert offer().status_code == 409
    if initial_status != "accepted":
        command["expectedVersion"] = 4
        assert offer().status_code == 200
    with store.engine.connect() as conn:
        reserved = conn.execute(
            text("""
            SELECT count(*), sum(capacity_kg) FROM c08_allocation.allocations
            WHERE run_id=:run AND resource_id=:vehicle AND status <> 'rejected'
        """),
            {"run": run_id, "vehicle": scenario.resource},
        ).one()
    assert reserved == (1, 10)


@pytest.mark.parametrize(
    "limit",
    [
        {"range_km": 1.4},
        {"operating_hours": [{"day": "friday", "opens": "08:00", "closes": "08:21"}]},
    ],
    ids=["range", "operating-hours"],
)
def test_incompatible_revision_releases_pending_offer(
    scenario, deliver, example, limit
):
    run_id = uuid4()
    initial = shared_plan().model_copy(update={"run_id": run_id, "event_id": uuid4()})
    seed_plan(
        scenario.store, initial, scenario.provider, scenario.resource, scenario.driver
    )
    deliver(initial)
    path = f"/api/v1/vehicle-assignments?runId={run_id}"
    command = {
        "planId": str(initial.payload.plan_id),
        "deliveryId": str(initial.payload.requirements[0].delivery_id),
        "expectedVersion": 3,
    }
    assert (
        scenario.client.post(
            path, headers=headers(scenario.operator), json=command
        ).status_code
        == 200
    )
    [offer] = scenario.client.get(path, headers=headers(scenario.driver)).json()
    # Both limits permit the original 6-minute route but exclude the detour.
    change = scenario.client.post(
        f"/api/v1/resources/{scenario.resource}",
        headers=headers(scenario.operator),
        json={
            "expected_version": 1,
            "details": {**PAYLOAD["details"], **limit},
        },
    )
    assert change.status_code == 200, change.text
    saved = example("revised-plan.json")
    saved["runScope"]["runId"] = str(run_id)
    save_demo_revision(scenario.store, saved, load_scenario().start_time)
    wire = example("plan-ready-revision-event.json")
    wire.update(runId=str(run_id), eventId=str(uuid4()))
    revision = type(initial).model_validate_json(json.dumps(wire))
    deliver(revision)
    deliver(revision)
    [row] = scenario.client.get(path, headers=headers(scenario.operator)).json()
    assert (row["status"], row["planVersion"], row["canOffer"]) == (
        "unresolved",
        4,
        False,
    )
    response = scenario.client.post(
        f"/api/v1/driver-offers/{offer['offerId']}/response?runId={run_id}",
        headers=headers(scenario.driver),
        json={"response": "accept"},
    )
    assert response.status_code == 409
    next_plan = new_plan().model_copy(update={"run_id": run_id, "scope": "simulation"})
    next_plan.payload.requirements[0].capacity.amount = 800
    seed_plan(
        scenario.store, next_plan, scenario.provider, scenario.resource, scenario.driver
    )
    assert scenario.store.consume(next_plan) == "allocated"
