"""Real PostgreSQL/RabbitMQ checks in the existing isolated CI services."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError


def test_owned_migrations_can_rollback_and_reapply(test_database_url):
    import psycopg

    folder = Path(__file__).resolve().parents[2] / "database/migrations"
    names = [
        "20261123160000_create_c11_observations.sql",
        "20261123160100_create_c12_replay_storage.sql",
    ]
    with psycopg.connect(test_database_url) as conn:
        for name in reversed(names):
            conn.execute((folder / name).read_text().split("-- migrate:down", 1)[1])
        for name in names:
            conn.execute(
                (folder / name)
                .read_text()
                .split("-- migrate:up", 1)[1]
                .split("-- migrate:down", 1)[0]
            )
        assert conn.execute(
            "select to_regclass('c12_simulation_replay.captures')"
        ).fetchone()[0]


def test_replay_sql_role_cannot_write_runs_or_operational_state(test_database_url):
    url = test_database_url.replace("postgres://", "postgresql+psycopg://")
    admin = create_engine(url)
    role = "replay_test_" + uuid4().hex
    limited = create_engine(url)
    try:
        with admin.begin() as conn:
            conn.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
            conn.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA c12_simulation_replay TO "{role}"'
            )
            conn.exec_driver_sql(
                f'GRANT SELECT ON c12_simulation_replay.runs TO "{role}"'
            )
            conn.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE ON c12_simulation_replay.sessions, "
                f'c12_simulation_replay.command_receipts TO "{role}"'
            )
            conn.exec_driver_sql(
                "GRANT SELECT, INSERT, DELETE ON "
                f'c12_simulation_replay.run_page_cursors TO "{role}"'
            )

        @event.listens_for(limited, "begin")
        def restrict(conn):
            conn.exec_driver_sql(f'SET LOCAL ROLE "{role}"')

        with limited.begin() as conn:
            conn.execute(text("SELECT run_id FROM c12_simulation_replay.runs LIMIT 1"))
            conn.execute(
                text(
                    "UPDATE c12_simulation_replay.sessions SET body = body WHERE false"
                )
            )
        for sql in [
            "UPDATE c12_simulation_replay.runs SET archive = archive WHERE false",
            "UPDATE c08_allocation.allocations SET status = status WHERE false",
            "UPDATE c03_resource_management.resources "
            "SET version = version WHERE false",
        ]:
            with pytest.raises(DBAPIError), limited.begin() as conn:
                conn.execute(text(sql))
    finally:
        limited.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP OWNED BY "{role}"')
            conn.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def test_real_broker_reaches_c11_store_and_deduplicates(test_database_url):
    broker = os.environ.get("TEST_RABBITMQ_URL")
    if not broker:
        pytest.skip("TEST_RABBITMQ_URL not set")
    import pika
    from sqlalchemy import select

    from app.components.c06_city_monitoring.publisher import RabbitMQPublisher
    from app.components.c11_kpi_analytics.store import ObservationStore, observations
    from app.shared.events import EventConsumer
    from tests.test_c11_c12_integration import source_event

    engine = create_engine(
        test_database_url.replace("postgres://", "postgresql+psycopg://")
    )
    exchange, queue = "analytics.test." + uuid4().hex, "analytics.test." + uuid4().hex
    store = ObservationStore(engine)
    message = source_event().model_copy(update={"event_id": uuid4()})
    publisher = RabbitMQPublisher(broker, exchange, {queue: [message.event_type]})
    consumer = EventConsumer(
        broker, queue, [message.event_type], store.consume, exchange
    )
    try:
        publisher.publish(message)
        publisher.publish(message)
        assert consumer.consume_available(max_messages=2) == 2
        with engine.connect() as conn:
            assert (
                len(
                    conn.execute(
                        select(observations).where(
                            observations.c.event_id == message.event_id
                        )
                    ).all()
                )
                == 1
            )
    finally:
        publisher.close()
        consumer.close()
        connection = pika.BlockingConnection(pika.URLParameters(broker))
        channel = connection.channel()
        channel.queue_delete(queue)
        channel.exchange_delete(exchange)
        connection.close()
        engine.dispose()


def test_real_broker_c04_trace_is_recorded_and_replayed_without_live_changes(
    test_database_url, monkeypatch
):
    """Real transports/storage; HTTP uses the explicitly trusted C02 test double."""
    broker = os.environ.get("TEST_RABBITMQ_URL")
    if not broker:
        pytest.skip("TEST_RABBITMQ_URL not set")
    import pika

    from app.components.analytics_demo import fixture_events, wait_for_recording
    from app.components.analytics_verify import verify_http
    from app.components.c06_city_monitoring.publisher import RabbitMQPublisher
    from app.components.c11_kpi_analytics import worker as c11_worker
    from app.components.c12_simulation_replay import worker as c12_worker
    from app.components.c12_simulation_replay.local_setup import (
        LOCAL_PASSWORD,
        provision,
    )
    from app.components.c12_simulation_replay.recording import CaptureRecorder
    from app.shared.auth import AuthContext
    from tests.test_c11_c12_integration import client

    engine = create_engine(
        test_database_url.replace("postgres://", "postgresql+psycopg://")
    )
    context = AuthContext(
        principal_id=uuid4(),
        principal_type="user",
        acting_provider_id=uuid4(),
        provider_ids=[],
        roles=["analyst"],
        platform_roles=[],
        correlation_id=uuid4(),
    )
    context = context.model_copy(update={"provider_ids": [context.acting_provider_id]})
    now, run_id, delivery_id = datetime.now(UTC), uuid4(), uuid4()
    events = fixture_events(
        run_id, delivery_id, context.correlation_id, now, context.acting_provider_id
    )
    exchange = "analytics.trace." + uuid4().hex
    queues = {
        exchange + ".c11": [e.event_type for e in events],
        exchange + ".c12": [e.event_type for e in events],
    }
    recorder = CaptureRecorder(engine)
    monkeypatch.setenv("RABBITMQ_EXCHANGE", exchange)
    # Isolate test queues, but use the actual worker factories/configuration.
    consumers = []
    for worker, (queue, types) in zip(
        [c11_worker, c12_worker], queues.items(), strict=True
    ):
        monkeypatch.setattr(worker, "QUEUE", queue)
        monkeypatch.setitem(worker.SUBSCRIBER_QUEUES, queue, types)
        consumers.append(worker.make_consumer(engine, broker))
    # The publisher must not create subscriber bindings: routing now proves
    # that each worker read RABBITMQ_EXCHANGE and declared its own binding.
    publisher = RabbitMQPublisher(broker, exchange)
    playback_role = "replay_login_test_" + uuid4().hex
    restricted = create_engine(
        engine.url.set(username=playback_role, password=LOCAL_PASSWORD)
    )
    try:
        for consumer in consumers:
            consumer.declare()
        recorder.begin(
            run_id,
            context.acting_provider_id,
            now - timedelta(minutes=2),
            {"initialState": {"deliveries": {}}},
            True,
        )
        for event in events:
            publisher.publish(event)
            publisher.publish(event)  # both consumers must deduplicate retries
        for consumer in consumers:
            assert consumer.consume_available(max_messages=4) == 4
        wait_for_recording(engine, run_id, events, timeout=0)
        recorder.seal(run_id, now + timedelta(minutes=1))
        import psycopg

        # The exact Compose provisioning path is repeatable, uses a real login,
        # and supplies the HTTP adapter's separate restricted replay connection.
        for _ in range(2):
            with psycopg.connect(test_database_url) as conn:
                provision(conn, role=playback_role, expected_database="ulop_test")
        with restricted.connect() as conn:
            assert (
                conn.execute(text("SELECT current_user")).scalar_one() == playback_role
            )

        def live_snapshot():
            with engine.connect() as conn:
                return [
                    conn.execute(
                        text(
                            "SELECT row_to_json(t)::text AS body "
                            f"FROM {table} t ORDER BY body"
                        )
                    )
                    .scalars()
                    .all()
                    for table in [
                        "c03_resource_management.resources",
                        "c08_allocation.allocations",
                        "c12_simulation_replay.captures",
                        "c12_simulation_replay.runs",
                    ]
                ]

        before = live_snapshot()

        def no_publish(*args, **kwargs):
            pytest.fail("Replay attempted to publish an event")

        monkeypatch.setattr(RabbitMQPublisher, "publish", no_publish)
        with client(engine, context, replay_engine=restricted) as http:
            http.app.state.settings.shared_run_ids = (run_id,)

            def call(method, path, body=None, key=None):
                response = http.request(
                    method,
                    path,
                    json=body,
                    headers={"Idempotency-Key": key} if key else {},
                )
                assert response.is_success, response.text
                return response.json()

            verify_http(call, context.acting_provider_id, run_id, events, now)
        assert live_snapshot() == before
        for table, column in [
            ("c12_simulation_replay.runs", "archive"),
            ("c12_simulation_replay.captures", "body"),
            ("c04_delivery_management.deliveries", "version"),
            ("c08_allocation.allocations", "status"),
        ]:
            with pytest.raises(DBAPIError), restricted.begin() as conn:
                conn.execute(
                    text(f"UPDATE {table} SET {column} = {column} WHERE false")
                )
        # An unexpected existing grant must abort setup rather than silently
        # treating a broader login as the least-privilege playback identity.
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "GRANT SELECT ON c04_delivery_management.deliveries "
                f'TO "{playback_role}"'
            )
        with pytest.raises(ValueError, match="unexpected component access"):
            with psycopg.connect(test_database_url) as conn:
                provision(conn, role=playback_role, expected_database="ulop_test")
    finally:
        restricted.dispose()
        with engine.begin() as conn:
            if conn.execute(
                text("select 1 from pg_roles where rolname = :role"),
                {"role": playback_role},
            ).first():
                conn.exec_driver_sql(f'DROP OWNED BY "{playback_role}"')
                conn.exec_driver_sql(f'DROP ROLE "{playback_role}"')
        publisher.close()
        for consumer in consumers:
            consumer.close()
        connection = pika.BlockingConnection(pika.URLParameters(broker))
        channel = connection.channel()
        for queue in queues:
            channel.queue_delete(queue)
        channel.exchange_delete(exchange)
        connection.close()
        engine.dispose()
