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
from app.demo.c08 import new_plan, seed_plan, shared_plan
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


@pytest.mark.skipif(
    not os.getenv("TEST_RABBITMQ_URL"), reason="needs a test RabbitMQ broker"
)
@pytest.mark.parametrize(
    "plan_factory", [new_plan, shared_plan], ids=["live", "dublin"]
)
def test_plan_ready_to_driver_acceptance(scenario, plan_factory):
    broker = os.environ["TEST_RABBITMQ_URL"]
    client, store = scenario.client, scenario.store
    operator, driver, other = scenario.operator, scenario.driver, scenario.other
    event = plan_factory()
    exchange, queue = f"c08.test.{uuid4()}", f"c08.test.{uuid4()}"
    publisher = RabbitMQPublisher(broker, exchange, {queue: ["PlanReady"]})

    def consume_one():
        consumer = make_consumer(store, broker, exchange, queue)
        try:
            consumer.declare()
            assert consumer.consume_available(max_messages=1) == 1
        finally:
            consumer.close()

    query = f"?runId={event.run_id}" if event.run_id else ""
    live_path = "/api/v1/vehicle-assignments"
    path = live_path + query
    try:
        seed_plan(store, event, scenario.provider, scenario.resource, driver)
        seed_plan(
            store, event, scenario.provider, scenario.resource, driver
        )  # rerun demo
        old = json.loads(event.model_dump_json(by_alias=True))
        old["eventId"] = str(uuid4())
        old["payload"]["planVersion"] -= 1
        old["entityVersions"][str(event.payload.plan_id)] -= 1
        publisher.publish(type(event).model_validate_json(json.dumps(old)))
        consume_one()
        assert client.get(path, headers=headers(operator)).json() == []
        publisher.publish(event)
        consume_one()
        publisher.publish(event)
        consume_one()  # New consumer connection, same durable state.
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
        assert (
            client.get(path, headers=headers(driver)).json()[0]["status"] == "offered"
        )
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
        assert (
            client.post(path, headers=offer_headers, json=command).json()
            == offer.json()
        )
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
    finally:
        publisher.close()
        with pika.BlockingConnection(pika.URLParameters(broker)) as connection:
            channel = connection.channel()
            channel.queue_delete(queue)
            channel.exchange_delete(exchange)


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
