import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.components.c02_identity_access.tokens import SupabaseVerifier
from app.components.c04_delivery_management.router import router as deliveries_router
from app.components.c04_delivery_management.service import DeliveryService
from app.components.c05_planning.delivery_gateway import DeliveryPlanningGateway
from app.components.c05_planning.router import router as planning_router
from app.components.c05_planning.service import PlanningService
from app.components.c05_planning.store import PostgresPlanStore
from app.components.c06_city_monitoring.ingestion import CityMonitoring
from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c06_city_monitoring.router import libpq_url
from app.components.c06_city_monitoring.router import router as city_state_router
from app.components.c07_disruption_detection.postgres_store import (
    PostgresDisruptionStore,
)
from app.components.c07_disruption_detection.router import router as disruptions_router
from app.components.c08_allocation.router import router as allocation_router
from app.components.c09_logistics_knowledge.demo_corpus import DEMO_ZONES
from app.components.c09_logistics_knowledge.postgres_store import (
    PostgresKnowledgeStore,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.router import router as decisions_router
from app.components.c10_decision_records.service import DecisionRecords
from app.components.c11_kpi_analytics.router import router as analytics_router
from app.components.c12_simulation_replay.router import router as replay_router
from app.components.resources.router import router
from app.components.resources.service import ResourceService
from app.config import get_ingress_settings, get_settings
from app.demo.feed import load_scenario
from app.infrastructure.database import make_engine
from app.infrastructure.errors import APIError, api_error_handler
from app.integrations.identity import router as identity_router
from app.integrations.mocks import LOCATIONS
from app.shared.logging import configure_logging

STATIC = Path(__file__).parent / "static"
logger = logging.getLogger("urban.resources")


def create_app(
    engine=None,
    settings=None,
    *,
    identity_verifier=None,
    city_state_store=None,
    disruption_store=None,
    deliveries=None,
    planning_gateway=None,
    clock=None,
):
    @asynccontextmanager
    async def lifespan(app):
        configure_logging("API")
        app.state.settings = settings or get_settings()
        if not app.state.settings.mock_dependencies:
            raise RuntimeError(
                "Real mapping/C08 adapters are not implemented. Keep this local."
            )
        project_url = app.state.settings.supabase_url
        app.state.identity_verifier = identity_verifier or (
            SupabaseVerifier(str(project_url)) if project_url else None
        )
        database_url = app.state.settings.database_url.get_secret_value()
        conninfo = libpq_url(database_url)
        service_clock = clock or (lambda: datetime.now(UTC))
        app.state.engine = engine or make_engine(database_url)
        app.state.resources = ResourceService(
            app.state.engine, app.state.settings.availability_max_age_seconds
        )
        replay_url = getattr(app.state.settings, "replay_database_url", None)
        app.state.replay_engine = (
            make_engine(replay_url.get_secret_value()) if replay_url else None
        )
        app.state.city_monitoring = CityMonitoring(
            store=city_state_store or PostgresCityStateStore(conninfo),
            clock=service_clock,
        )
        app.state.disruption_store = disruption_store or PostgresDisruptionStore(
            conninfo
        )
        # C09 runs in-process; C05 calls app.state.knowledge.query_knowledge
        app.state.knowledge = LogisticsKnowledge(
            PostgresKnowledgeStore(conninfo, DEMO_ZONES),
            clock=service_clock,
        )
        # C10 runs on its caller's connection; its HTTP reads open their own
        app.state.decisions = DecisionRecords(
            PostgresDecisionStore(), clock=service_clock
        )
        app.state.decision_connection = partial(
            psycopg.connect, conninfo, connect_timeout=5
        )
        app.state.dublin_scenario = load_scenario()
        app.state.planning = PlanningService(
            PostgresPlanStore(conninfo),
            app.state.knowledge,
            app.state.decisions,
            app.state.dublin_scenario,
            clock=service_clock,
        )
        app.state.deliveries = deliveries or DeliveryService(
            app.state.engine,
            planning_gateway=planning_gateway
            or DeliveryPlanningGateway(app.state.planning),
            clock=service_clock,
        )
        yield
        if app.state.replay_engine is not None:
            app.state.replay_engine.dispose()
        if engine is None:
            app.state.engine.dispose()

    app = FastAPI(
        title="Urban Logistics · UC01 Resource API",
        version="0.1.0",
        description="C02 identity, persistent resources, mock mapping and C08. "
        "C05 generates simulation proposals with C09 policy evidence and C10 "
        "decision records. Resource registration uses a durable audit stand-in.",
        lifespan=lifespan,
    )
    allowed_hosts, cors_origins = get_ingress_settings(settings)

    @app.middleware("http")
    async def request_context(request, call_next):
        supplied = request.headers.get("X-Correlation-ID", "")
        try:
            request.state.correlation_id = str(UUID(supplied))
        except ValueError:
            request.state.correlation_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Acting-Provider",
                "X-Correlation-ID",
            ],
            expose_headers=[
                "Idempotency-Replayed",
                "Location",
                "X-Correlation-ID",
                "X-Plan-Id",
                "X-Plan-Stub",
                "X-Plan-Version",
            ],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    app.add_exception_handler(APIError, api_error_handler)

    async def validation_error(request, error):
        fields = [
            {
                "path": ".".join(map(str, entry["loc"])),
                "message": entry["msg"],
                "code": entry["type"],
            }
            for entry in error.errors()
        ]
        return await api_error_handler(
            request,
            APIError(
                422,
                "VALIDATION_FAILED",
                "Correct the highlighted fields.",
                fields=fields,
            ),
        )

    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(ValidationError, validation_error)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        logger.error(
            "Database operation failed correlation_id=%s type=%s",
            request.state.correlation_id,
            type(error).__name__,
        )
        return await api_error_handler(
            request,
            APIError(
                503,
                "SUBMISSION_UNCONFIRMED",
                "The database result could not be confirmed. Retry with the same key.",
            ),
        )

    @app.get("/health", tags=["Development"])
    def health():
        with app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "dependencies": "mock", "database": "connected"}

    @app.get("/api/v1/development/config", tags=["Development"])
    def development_config():
        return {
            "locations": LOCATIONS,
            "dependencies": "mock",
            "notice": "Synthetic test inputs are saved in the configured database.",
        }

    @app.get("/", include_in_schema=False)
    def test_page():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(allocation_router)
    app.include_router(planning_router)
    app.include_router(identity_router)
    app.include_router(router)
    app.include_router(deliveries_router)
    app.include_router(city_state_router)
    app.include_router(analytics_router)
    app.include_router(replay_router)
    app.include_router(disruptions_router)
    app.include_router(decisions_router)
    return app


app = create_app()
