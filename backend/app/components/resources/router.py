from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from app.components.resources.schemas import (
    ActivationRequest,
    ProfileStatus,
    ResourceCreate,
    ResourceList,
    ResourceResponse,
    ResourceType,
    ResourceUpdate,
)
from app.infrastructure.errors import correlation_id
from app.integrations.identity import AuthContext, authenticate, require_manager
from app.integrations.mocks import (
    AgentOutcome,
    LocationOutcome,
    MockAgentGateway,
    MockLocationGateway,
)

router = APIRouter(prefix="/api/v1/resources", tags=["UC01 resources"])
Identity = Annotated[AuthContext, Depends(authenticate)]
Key = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
LocationScenario = Annotated[LocationOutcome, Header(alias="X-Mock-Location-Result")]
AgentScenario = Annotated[AgentOutcome, Header(alias="X-Mock-Agent-Result")]
ERRORS = {
    401: {"description": "Authentication missing or invalid"},
    403: {"description": "Role does not permit this operation"},
    404: {"description": "Resource missing or outside authorised provider"},
    409: {"description": "Duplicate identifier, stale version, or key reused"},
    422: {"description": "Invalid fields, details, or unmatched location"},
    503: {"description": "Database or required dependency unavailable"},
}


def command_response(result):
    status, body, replayed = result
    headers = {"Idempotency-Replayed": str(replayed).lower()}
    if status == 201:
        headers["Location"] = f"/api/v1/resources/{body['id']}"
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.post("", status_code=201, response_model=ResourceResponse, responses=ERRORS)
def register_resource(
    body: ResourceCreate,
    request: Request,
    context: Identity,
    idempotency_key: Key,
    location_result: LocationScenario = "matched",
    agent_result: AgentScenario = "active",
):
    """Register a validated resource and evaluate mock agent activation.

    Location failure stores nothing. Agent failure preserves an ineligible resource.
    Mock outcomes are selected with development-only headers, not payload fields.
    """
    require_manager(context)
    return command_response(
        request.app.state.resources.create(
            context,
            body,
            idempotency_key,
            correlation_id(request),
            MockLocationGateway(location_result),
            MockAgentGateway(agent_result),
        )
    )


@router.get("", response_model=ResourceList, responses=ERRORS)
def list_resources(
    request: Request,
    context: Identity,
    resource_type: ResourceType | None = None,
    profile_status: ProfileStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
):
    return request.app.state.resources.list(
        context, resource_type, profile_status, limit, cursor
    )


@router.get("/{resource_id}", response_model=ResourceResponse, responses=ERRORS)
def get_resource(resource_id: UUID, request: Request, context: Identity):
    return request.app.state.resources.get(context, resource_id)


@router.post("/{resource_id}", response_model=ResourceResponse, responses=ERRORS)
def update_resource(
    resource_id: UUID,
    body: ResourceUpdate,
    request: Request,
    context: Identity,
    idempotency_key: Key,
    location_result: LocationScenario = "matched",
    agent_result: AgentScenario = "active",
):
    """Update using expected_version. Supplied details replace the complete object.

    Resource type and all identity/audit/activation fields are server controlled.
    """
    require_manager(context)
    return command_response(
        request.app.state.resources.update(
            context,
            resource_id,
            body,
            idempotency_key,
            correlation_id(request),
            MockLocationGateway(location_result),
            MockAgentGateway(agent_result),
        )
    )


@router.post(
    "/{resource_id}/agent-activation",
    response_model=ResourceResponse,
    responses={**ERRORS, 202: {"description": "Activation remains pending"}},
)
def activate_agent(
    resource_id: UUID,
    body: ActivationRequest,
    request: Request,
    context: Identity,
    idempotency_key: Key,
    agent_result: AgentScenario = "active",
):
    """Request activation or retry; an active agent is reused without a version bump."""
    require_manager(context)
    return command_response(
        request.app.state.resources.activate(
            context,
            resource_id,
            body,
            idempotency_key,
            correlation_id(request),
            MockAgentGateway(agent_result),
        )
    )
