"""The previous #119 database must replay through C04's canonical command store."""

import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.components.c04_delivery_management.planning import (
    C05_SERVICE_PRINCIPAL_ID,
    seed_demo_delivery,
)
from app.components.c04_delivery_management.service import DeliveryPlanningService
from app.shared.auth import AuthContext
from app.shared.contracts import RunScopeSelector
from app.shared.result import Ok
from tests.conftest import MIGRATIONS_DIR, _migrate_up_sql


def test_legacy_delivery_and_multi_request_link_replay(c04_database, example):
    run_id, plan_id = uuid4(), uuid4()
    ids = sorted((uuid4(), uuid4()), reverse=True)
    request = {
        "planId": str(plan_id),
        "requestIds": list(map(str, ids)),
        "expectedVersions": {str(i): 1 for i in ids},
    }
    receipt = {**request, "versions": {str(i): 2 for i in ids}}
    del receipt["expectedVersions"]
    engine = create_engine(
        make_url(c04_database).set(drivername="postgresql+psycopg"), poolclass=NullPool
    )
    # All DDL and fixture data roll back when this connection closes.
    with engine.connect() as link:
        link.begin()
        conn = link.connection.driver_connection
        conn.execute("create schema c04_previous_demo")
        conn.execute(
            """create table c04_previous_demo.deliveries as
               select run_id, delivery_id, delivery, plan_id, initial_snapshot_hash
                 from c04_delivery_management.deliveries with no data;
               create table c04_previous_demo.planning_receipts (
                 run_id uuid, principal_id text, idempotency_key text,
                 request_body jsonb, receipt jsonb);
               alter table c04_delivery_management.deliveries
                 drop column initial_snapshot_hash"""
        )
        for did in ids:
            original = {**example("delivery-submitted.json"), "deliveryId": str(did)}
            saved = {
                **original,
                "status": "planned",
                "version": 2,
                "plannedAt": original["updatedAt"],
            }
            fingerprint = hashlib.sha256(
                json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
            ).digest()
            conn.execute(
                "insert into c04_previous_demo.deliveries values (%s,%s,%s,%s,%s)",
                (run_id, did, Jsonb(saved), plan_id, fingerprint),
            )
        conn.execute(
            "insert into c04_previous_demo.planning_receipts "
            "values (%s,'service:c05','original-key',%s,%s)",
            (run_id, Jsonb(request), Jsonb(receipt)),
        )
        import_migration = (
            MIGRATIONS_DIR / "20261124100100_import_c04_demo_provenance.sql"
        )
        conn.execute(_migrate_up_sql(import_migration))
        # A downgrade and reapply must restore the original hash, including
        # when the canonical delivery has advanced since its first import.
        down = import_migration.read_text(encoding="utf-8").split("-- migrate:down")[1]
        conn.execute(down)
        conn.execute(_migrate_up_sql(import_migration))
        # Loading the old input again leaves the linked, newer delivery untouched.
        seed_demo_delivery(conn, run_id, original)
        assert (
            conn.execute(
                "select status, version, plan_id "
                "from c04_delivery_management.deliveries where run_id=%s",
                (run_id,),
            ).fetchall()
            == [("planned", 2, plan_id)] * 2
        )
        caller = AuthContext(
            principal_id=C05_SERVICE_PRINCIPAL_ID,
            principal_type="service",
            service_id="C05",
            roles=("service",),
            provider_ids=(),
            platform_roles=(),
            correlation_id=uuid4(),
        )
        replay = DeliveryPlanningService(engine).mark_requests_planned(
            caller,
            RunScopeSelector(scope="simulation", run_id=run_id),
            ids,
            plan_id,
            {i: 1 for i in ids},
            "original-key",
            connection=link,
        )
        assert isinstance(replay, Ok), replay
        assert replay.value.model_dump(mode="json", by_alias=True) == receipt

        # On a downgrade below the C04 migration the old feature schema
        # comes back with the same links, fingerprints and idempotency input.
        conn.execute(down)
        for migration in (
            "20261124100000_create_c04_delivery_management.sql",
            "20261124095959_stage_previous_c04_demo.sql",
        ):
            sql = (MIGRATIONS_DIR / migration).read_text(encoding="utf-8")
            conn.execute(sql.split("-- migrate:down")[1])
        assert conn.execute(
            "select count(*) from c04_delivery_management.deliveries "
            "where run_id=%s and plan_id=%s and initial_snapshot_hash is not null",
            (run_id, plan_id),
        ).fetchone() == (2,)
        assert conn.execute(
            "select request_body, receipt "
            "from c04_delivery_management.planning_receipts "
            "where run_id=%s and idempotency_key='original-key'",
            (run_id,),
        ).fetchone() == (request, receipt)
