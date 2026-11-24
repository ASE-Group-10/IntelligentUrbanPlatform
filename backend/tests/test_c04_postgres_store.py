"""Postgres-backed tests for C04 Delivery Management migration and store.

Skipped unless ``TEST_DATABASE_URL`` names a dedicated ``*_test`` database
(automatically configured in CI).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select

from app.components.c04_delivery_management import store as c04_store
from app.components.c04_delivery_management.contracts import (
    DeliveryEvent,
    DeliveryProgress,
    SubmitDeliveryRequest,
)
from app.components.c04_delivery_management.service import DeliveryService
from app.shared.auth import AuthContext
from app.shared.contracts import RunScopeSelector
from app.shared.result import Failure, Ok

DEMO_RUN = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")


def _sqlalchemy_url(libpq_url: str) -> str:
    if libpq_url.startswith("postgres://"):
        return libpq_url.replace("postgres://", "postgresql+psycopg://", 1)
    if libpq_url.startswith("postgresql://"):
        return libpq_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return libpq_url


def test_c04_postgres_submit_plan_link_and_progress(c04_database, example):
    engine = create_engine(_sqlalchemy_url(c04_database))
    try:
        fixed_now = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
        service = DeliveryService(engine, clock=lambda: fixed_now)

        provider_id = uuid4()
        operator_ctx = AuthContext(
            principal_id=uuid4(),
            principal_type="user",
            acting_provider_id=provider_id,
            roles=("operator",),
            provider_ids=(provider_id,),
            platform_roles=(),
            correlation_id=uuid4(),
        )
        sim_scope = RunScopeSelector(scope="simulation", run_id=DEMO_RUN)
        live_scope = RunScopeSelector(scope="live")
        cmd = SubmitDeliveryRequest.model_validate(
            example("submit-delivery-request.json")
        )

        submitted = service.submit_delivery(
            operator_ctx, sim_scope, cmd, operator_ctx.correlation_id
        )
        assert submitted.status_code == 201
        assert submitted.plan is not None and submitted.plan.stub is True

        replayed = service.submit_delivery(
            operator_ctx, sim_scope, cmd, operator_ctx.correlation_id
        )
        assert replayed.status_code == 200
        assert replayed.delivery.delivery_id == submitted.delivery.delivery_id

        # Strict run isolation in Postgres: live cannot read simulation delivery.
        c05_ctx = AuthContext(
            principal_id=UUID("00000000-0000-4000-8000-000000000005"),
            principal_type="service",
            service_id="C05",
            roles=("service",),
            provider_ids=(),
            platform_roles=(),
            correlation_id=operator_ctx.correlation_id,
        )
        assert service.get_requests(
            c05_ctx, live_scope, [submitted.delivery.delivery_id]
        ) == Failure("NotFound", "C04.DELIVERY_NOT_FOUND")

        plan_id = uuid4()
        linked = service.mark_requests_planned(
            c05_ctx,
            sim_scope,
            [submitted.delivery.delivery_id],
            plan_id,
            {submitted.delivery.delivery_id: 1},
            "pg-link-1",
        )
        assert isinstance(linked, Ok)
        assert linked.value.versions == {submitted.delivery.delivery_id: 2}

        occurred = datetime(2026, 11, 20, 10, 30, 0, tzinfo=UTC)
        delivered = service.update_progress(
            c05_ctx,
            sim_scope,
            submitted.delivery.delivery_id,
            DeliveryProgress(status="delivered", updated_at=occurred, note="Delivered"),
            "pg-prog-1",
        )
        assert isinstance(delivered, Ok)
        assert delivered.value.version == 3
        assert delivered.value.status == "delivered"

        with engine.connect() as conn:
            events = conn.execute(select(c04_store.outbox)).mappings().all()
        assert len(events) == 2
        for row in events:
            DeliveryEvent.model_validate(row["envelope"])
    finally:
        engine.dispose()
