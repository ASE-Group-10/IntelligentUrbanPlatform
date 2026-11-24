"""Read a saved C05 plan within the operator's authorised scope."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import UUID4, ValidationError

from app.infrastructure.errors import APIError, correlation_id
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext, require_role
from app.shared.contracts import ContractModel
from app.shared.result import Failure
from app.shared.run_access import require_run_access

from .models import Plan, PlanScope


class PlanReadError(ContractModel):
    code: Literal["Invalid", "Unauthenticated", "Forbidden", "NotFound", "Unavailable"]
    message: str
    correlation_id: UUID


class PlanReadErrorResponse(ContractModel):
    error: PlanReadError


_ERROR_CODES = {
    400: "Invalid",
    401: "Unauthenticated",
    403: "Forbidden",
    404: "NotFound",
    503: "Unavailable",
}


class PlanningRoute(APIRoute):
    """Translate shared identity and validation errors to the C05 HTTP contract."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                status, message = 400, "Invalid plan identifier or run scope."
            except APIError as exc:
                status = 400 if exc.status == 422 else exc.status
                message = exc.message
            body = PlanReadErrorResponse(
                error=PlanReadError(
                    code=_ERROR_CODES[status],
                    message=message,
                    correlation_id=correlation_id(request),
                )
            )
            return JSONResponse(
                body.model_dump(mode="json", by_alias=True), status_code=status
            )

        return handle


router = APIRouter(
    prefix="/api/v1/plans",
    tags=["C05 Planning and Replanning"],
    route_class=PlanningRoute,
)


@router.get(
    "/{planId}",
    response_model=Plan,
    response_model_exclude_none=True,
    operation_id="c05GetPlan",
    # A default client error also suppresses FastAPI's automatic 422 schema:
    # invalid parameters on this operation use the C05 400 envelope.
    responses={
        status: {"model": PlanReadErrorResponse} for status in (*_ERROR_CODES, "4XX")
    },
)
def get_plan(
    request: Request,
    plan_id: Annotated[UUID4, Path(alias="planId")],
    scope: Literal["live", "simulation"],
    context: Annotated[AuthContext, Depends(authenticate)],
    run_id: Annotated[UUID4 | None, Query(alias="runId")] = None,
) -> Plan:
    if isinstance(require_role(context, "operator"), Failure):
        raise APIError(403, "Forbidden", "Operator access is required.")
    try:
        selector = PlanScope(scope=scope, run_id=run_id)
    except ValidationError as exc:
        raise APIError(
            400, "Invalid", "Invalid combination of scope and runId."
        ) from exc
    require_run_access(request, run_id)
    result = request.app.state.planning.get_plan(selector, plan_id)
    if isinstance(result, Failure):
        if result.kind == "NotFound":
            raise APIError(404, "NotFound", "Plan not found in the selected scope.")
        raise APIError(503, "Unavailable", "The plan store is unavailable.")
    return result.value
