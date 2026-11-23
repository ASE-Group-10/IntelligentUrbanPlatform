"""Real PostgreSQL/RabbitMQ checks in the existing isolated CI services."""

import os
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
