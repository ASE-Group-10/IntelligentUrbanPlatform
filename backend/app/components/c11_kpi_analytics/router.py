"""C01 HTTP adapter using verified C02 identity and C11-owned read models."""

from datetime import UTC, datetime
from threading import Lock
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.components.c11_kpi_analytics.contracts import Breakdown, KPIQuery, KPISet
from app.components.c11_kpi_analytics.query import KPIAnalytics
from app.components.c11_kpi_analytics.store import ObservationSource
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext, require_role
from app.shared.http_outcomes import ContractRoute, outcome
from app.shared.result import Failure

router = APIRouter(
    prefix="/api/v1/analytics/kpis",
    tags=["C11 KPI Analytics"],
    route_class=ContractRoute,
)
Identity = Annotated[AuthContext, Depends(authenticate)]
_setup_lock = Lock()


def allowed(context, scope, shared_runs):
    return not isinstance(
        require_role(context, "analyst", scope.provider_id), Failure
    ) and (scope.scope == "live" or scope.run_id in shared_runs)


def service(request):
    with _setup_lock:
        if not hasattr(request.app.state, "c11_analytics"):
            shared_runs = request.app.state.settings.shared_run_ids
            request.app.state.c11_analytics = KPIAnalytics(
                ObservationSource(request.app.state.engine),
                lambda context, scope: allowed(context, scope, shared_runs),
                lambda: datetime.now(UTC),
            )
    return request.app.state.c11_analytics


@router.post("/query", response_model=KPISet, operation_id="c11QueryKPIs")
def query_kpis(body: KPIQuery, request: Request, context: Identity):
    return outcome(request, service(request).query_kpis(context, body))


@router.post(
    "/{metric_id}/breakdown",
    response_model=Breakdown,
    operation_id="c11GetKPIBreakdown",
)
def breakdown(
    body: KPIQuery,
    metric_id: str,
    request: Request,
    context: Identity,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
):
    shared_runs = request.app.state.settings.shared_run_ids
    analytics = KPIAnalytics(
        ObservationSource(request.app.state.engine, context.principal_id),
        lambda ctx, scope: allowed(ctx, scope, shared_runs),
        lambda: datetime.now(UTC),
    )
    return outcome(
        request, analytics.get_kpi_breakdown(context, body, metric_id, limit, cursor)
    )
