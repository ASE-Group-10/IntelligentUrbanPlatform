import json
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request

from app.infrastructure.errors import APIError
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext
from app.shared.contracts import ContractModel, Version

from .models import AllocationOutcome
from .store import AllocationStore

router = APIRouter(prefix="/api/v1", tags=["C08 Allocation and Resource Agents"])
Identity = Annotated[AuthContext, Depends(authenticate)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class AssignmentRequest(ContractModel):
    plan_id: UUID
    delivery_id: UUID
    expected_version: Version


class OfferResponse(ContractModel):
    response: Literal["accept", "reject"]


class AssignmentView(ContractModel):
    plan_id: UUID
    plan_version: int
    delivery_id: UUID
    assignment_ref: UUID
    resource_id: UUID
    resource_name: str
    offer_id: UUID | None
    status: Literal["allocated", "offered", "accepted", "rejected"]
    correlation_id: UUID
    can_offer: bool
    can_respond: bool


def store(request):
    return AllocationStore(
        request.app.state.engine,
        request.app.state.settings.availability_max_age_seconds,
    )


def is_operator(ctx):
    return "operator" in ctx.roles or "admin" in ctx.platform_roles


def denied():
    return APIError(403, "FORBIDDEN", "Access denied.")


@router.get(
    "/vehicle-assignments",
    response_model=list[AssignmentView],
    operation_id="listVehicleAssignments",
)
def list_assignments(request: Request, ctx: Identity):
    operator = is_operator(ctx)
    if not operator and "driver" not in ctx.roles:
        raise denied()
    views = []
    for entry in store(request).assignments(ctx, operator):
        data = dict(entry)
        recipient = data.pop("driver_id") == ctx.principal_id and "driver" in ctx.roles
        views.append(
            AssignmentView(
                **data,
                can_offer=operator and data["status"] == "allocated",
                can_respond=recipient and data["status"] == "offered",
            )
        )
    return views


def traced(request, result):
    request.state.correlation_id = result["correlationId"]
    logging.getLogger("uvicorn.error").info(
        "C08 assignmentRef=%s outcome=%s correlationId=%s",
        result["assignmentRef"],
        result["code"],
        result["correlationId"],
    )
    return AllocationOutcome.model_validate_json(json.dumps(result))


@router.post(
    "/vehicle-assignments",
    response_model=AllocationOutcome,
    response_model_exclude_none=True,
    operation_id="assignVehicle",
)
def assign_vehicle(body: AssignmentRequest, request: Request, ctx: Identity, key: Key):
    if not is_operator(ctx):
        raise denied()
    return traced(request, store(request).offer(ctx, body, key))


@router.post(
    "/driver-offers/{offerId}/response",
    response_model=AllocationOutcome,
    response_model_exclude_none=True,
    operation_id="recordOfferResponse",
)
def record_offer_response(
    offer_id: Annotated[UUID, Path(alias="offerId")],
    body: OfferResponse,
    request: Request,
    ctx: Identity,
    key: Key,
):
    if "driver" not in ctx.roles:
        raise denied()
    return traced(request, store(request).respond(ctx, offer_id, body.response, key))
