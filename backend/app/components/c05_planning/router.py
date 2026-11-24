"""Operator reads and confirmation of a saved C05 proposal."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import UUID4, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.infrastructure.errors import APIError, correlation_id
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext, require_role
from app.shared.contracts import ContractModel
from app.shared.result import Failure
from app.shared.run_access import require_run_access

from .models import Plan, PlanScope


class ConfirmationRequest(ContractModel):
    expected_version: Annotated[int, Field(strict=True, ge=1)]
    run_scope: PlanScope


class PlanError(ContractModel):
    code: Literal[
        "Invalid", "Unauthenticated", "Forbidden", "NotFound", "Conflict", "Unavailable"
    ]
    message: str
    correlation_id: UUID


class PlanErrorResponse(ContractModel):
    error: PlanError


_ERROR_CODES = {
    400: "Invalid",
    401: "Unauthenticated",
    403: "Forbidden",
    404: "NotFound",
    409: "Conflict",
    503: "Unavailable",
}


def _confirmation_actor(
    request: Request, context: Annotated[AuthContext, Depends(authenticate)]
) -> AuthContext:
    # Typed body/path/header validation can reject a request before the handler.
    request.state.confirmation_actor = context
    return context


def _requested_plan_id(request: Request) -> UUID | None:
    # Only store a well-formed identifier supplied by this caller, never raw input.
    try:
        plan_id = UUID(request.path_params["planId"])
    except ValueError:
        return None
    return plan_id if plan_id.version == 4 else None


def _confirmation_error(result: Failure) -> APIError:
    status = {
        "Invalid": 400,
        "Forbidden": 403,
        "NotFound": 404,
        "Conflict": 409,
    }.get(result.kind, 503)
    message = {
        400: "Invalid confirmation request.",
        403: "Operator access is required.",
        404: "The plan or one of its deliveries was not found in this scope.",
        409: "Confirmation conflicts with current state. Reload the plan.",
        503: "Confirmation is unavailable. Retry with the same key.",
    }[status]
    return APIError(status, _ERROR_CODES[status], message)


class PlanningRoute(APIRoute):
    """Translate shared identity and validation errors to the C05 HTTP contract."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                status, message = (
                    400,
                    "Check the plan identifier, scope and required fields.",
                )
                actor = getattr(request.state, "confirmation_actor", None)
                if actor is not None:
                    result = await run_in_threadpool(
                        request.app.state.planning.reject_confirmation,
                        actor,
                        _requested_plan_id(request),
                        Failure("Invalid", "C05.INVALID_CONFIRMATION"),
                    )
                    error = _confirmation_error(result)
                    status, message = error.status, error.message
            except APIError as exc:
                status = 400 if exc.status == 422 else exc.status
                message = exc.message
            body = PlanErrorResponse(
                error=PlanError(
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
        status: {"model": PlanErrorResponse}
        for status in (400, 401, 403, 404, 503, "4XX")
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
    result = request.app.state.planning.get_plan(selector, plan_id, operator=context)
    if isinstance(result, Failure):
        if result.kind == "NotFound":
            raise APIError(404, "NotFound", "Plan not found in the selected scope.")
        raise APIError(503, "Unavailable", "The plan store is unavailable.")
    return result.value


@router.post(
    "/{planId}/confirmation",
    response_model=Plan,
    response_model_exclude_none=True,
    operation_id="c05ConfirmPlan",
    responses={
        status: {"model": PlanErrorResponse} for status in (*_ERROR_CODES, "4XX")
    },
)
def confirm_plan(
    request: Request,
    plan_id: Annotated[UUID4, Path(alias="planId")],
    command: ConfirmationRequest,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
    context: Annotated[AuthContext, Depends(_confirmation_actor)],
) -> Plan:
    try:
        require_run_access(request, command.run_scope.run_id)
    except APIError as exc:
        result = request.app.state.planning.reject_confirmation(
            context, plan_id, Failure("Forbidden", "C05.RUN_FORBIDDEN")
        )
        if result.kind == "Unavailable":
            raise _confirmation_error(result) from exc
        raise
    result = request.app.state.planning.confirm_plan(
        context, command.run_scope, plan_id, command.expected_version, key
    )
    if isinstance(result, Failure):
        raise _confirmation_error(result)
    return result.value
