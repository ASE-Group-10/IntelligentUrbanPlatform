"""HTTP router for C04 Delivery Management (``openapi.yaml``).

Serves:
- ``POST /api/v1/deliveries`` (``c04SubmitDelivery``)
- ``GET /api/v1/deliveries`` (``c04GetRequests``)
- ``GET /api/v1/deliveries/{deliveryId}`` (``c04GetDelivery``)
- ``POST /api/v1/deliveries/{deliveryId}`` (``c04UpdateDelivery``)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse

from app.components.c04_delivery_management.contracts import (
    Delivery,
    DeliveryPage,
    DeliveryStatus,
    SubmitDeliveryRequest,
    UpdateDeliveryRequest,
)
from app.components.c04_delivery_management.service import (
    LocationOutcome,
    MockDeliveryLocationValidator,
)
from app.infrastructure.errors import correlation_id
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext
from app.shared.contracts import RunScopeSelector
from app.shared.run_access import require_run_access

router = APIRouter(prefix="/api/v1/deliveries", tags=["C04 Delivery Management"])

Identity = Annotated[AuthContext, Depends(authenticate)]
RunId = Annotated[
    UUID | None,
    Query(
        alias="runId",
        description="Simulation run partition. Omit for live deliveries.",
    ),
]
LocationScenario = Annotated[
    LocationOutcome,
    Header(alias="X-Mock-Location-Result", include_in_schema=False),
]

ERRORS = {
    400: {"description": "Request fields or cross-field constraints are invalid."},
    401: {"description": "Authentication is required."},
    403: {"description": "Authenticated principal is outside the requested scope."},
    404: {"description": "Delivery request was not found in the authorised scope."},
    409: {"description": "Idempotency or expected-version conflict."},
    422: {"description": "Request fields or cross-field constraints are invalid."},
    503: {"description": "Delivery state could not be read or committed."},
}


def _resolve_run_scope(request: Request, run_id: UUID | None) -> RunScopeSelector:
    require_run_access(request, run_id)
    if run_id is not None:
        return RunScopeSelector(scope="simulation", run_id=run_id)
    return RunScopeSelector(scope="live")


@router.post(
    "",
    status_code=201,
    response_model=Delivery,
    operation_id="c04SubmitDelivery",
    responses={
        200: {
            "description": (
                "Original result returned for an identical idempotent retry."
            ),
            "model": Delivery,
        },
        **{k: v for k, v in ERRORS.items() if k != 404},
    },
)
def submit_delivery(
    body: SubmitDeliveryRequest,
    request: Request,
    context: Identity,
    run_id: RunId = None,
    location_result: LocationScenario = "matched",
) -> JSONResponse:
    """Validate and persist a delivery request, then invoke C05 planning in-process."""
    run_scope = _resolve_run_scope(request, run_id)
    cid = UUID(correlation_id(request))
    outcome = request.app.state.deliveries.submit_delivery(
        context,
        run_scope,
        body,
        cid,
        MockDeliveryLocationValidator(location_result),
    )
    payload = outcome.delivery.to_wire()
    headers = {"Idempotency-Replayed": str(outcome.replayed).lower()}
    if outcome.status_code == 201:
        headers["Location"] = f"/api/v1/deliveries/{payload['deliveryId']}"
    if outcome.plan is not None:
        headers["X-Plan-Id"] = str(outcome.plan.plan_id)
        headers["X-Plan-Version"] = str(outcome.plan.version)
        if outcome.plan.stub:
            headers["X-Plan-Stub"] = "true"
    return JSONResponse(
        status_code=outcome.status_code, content=payload, headers=headers
    )


@router.get(
    "",
    response_model=DeliveryPage,
    operation_id="c04GetRequests",
    responses={k: v for k, v in ERRORS.items() if k in (401, 403, 422, 503)},
)
def list_deliveries(
    request: Request,
    context: Identity,
    status: DeliveryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    run_id: RunId = None,
) -> JSONResponse:
    """Return a bounded page of delivery requests in the caller's authorised scope."""
    run_scope = _resolve_run_scope(request, run_id)
    page = request.app.state.deliveries.list_deliveries(
        context, run_scope, status=status, limit=limit, cursor=cursor
    )
    return JSONResponse(
        status_code=200,
        content={
            "items": [item.to_wire() for item in page.items],
            "nextCursor": page.next_cursor,
        },
    )


@router.get(
    "/{deliveryId}",
    response_model=Delivery,
    operation_id="c04GetDelivery",
    responses={k: v for k, v in ERRORS.items() if k in (401, 403, 404, 503)},
)
def get_delivery(
    delivery_id: Annotated[UUID, Path(alias="deliveryId")],
    request: Request,
    context: Identity,
    run_id: RunId = None,
) -> JSONResponse:
    """Return one delivery request in the caller's authorised scope."""
    run_scope = _resolve_run_scope(request, run_id)
    delivery = request.app.state.deliveries.get_delivery(
        context, run_scope, delivery_id
    )
    return JSONResponse(status_code=200, content=delivery.to_wire())


@router.post(
    "/{deliveryId}",
    response_model=Delivery,
    operation_id="c04UpdateDelivery",
    responses=ERRORS,
)
def update_delivery(
    delivery_id: Annotated[UUID, Path(alias="deliveryId")],
    body: UpdateDeliveryRequest,
    request: Request,
    context: Identity,
    run_id: RunId = None,
    location_result: LocationScenario = "matched",
) -> JSONResponse:
    """Update a pending delivery request using optimistic concurrency control."""
    run_scope = _resolve_run_scope(request, run_id)
    cid = UUID(correlation_id(request))
    outcome = request.app.state.deliveries.update_delivery(
        context,
        run_scope,
        delivery_id,
        body,
        cid,
        MockDeliveryLocationValidator(location_result),
    )
    return JSONResponse(
        status_code=outcome.status_code,
        content=outcome.delivery.to_wire(),
        headers={"Idempotency-Replayed": str(outcome.replayed).lower()},
    )
