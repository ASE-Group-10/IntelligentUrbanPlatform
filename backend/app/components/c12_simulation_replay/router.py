"""Authorised, provider-bound replay HTTP controls. No recording write route."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import UUID4, Field

from app.components.c12_simulation_replay.contracts import (
    CannotReplay,
    ReplayControl,
    ReplayFrame,
    ReplayRequest,
)
from app.components.c12_simulation_replay.persistence import ReplayRepository
from app.integrations.identity import authenticate
from app.shared.auth import AuthContext, require_role
from app.shared.contracts import ContractModel, UtcDatetime
from app.shared.http_outcomes import ContractRoute, outcome
from app.shared.result import Failure

router = APIRouter(
    prefix="/api/v1", tags=["C12 Simulation and Replay"], route_class=ContractRoute
)
Identity = Annotated[AuthContext, Depends(authenticate)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


class ControlBody(ContractModel):
    command: Literal["play", "pause", "resume", "seek", "stop"]
    expected_cursor: int = Field(ge=0, strict=True)
    target_cursor: int | None = Field(default=None, ge=0, strict=True)


class AdvanceBody(ContractModel):
    expected_cursor: int = Field(ge=0, strict=True)


def repository(request, context):
    if isinstance(require_role(context, "analyst"), Failure):
        return Failure("Forbidden", "Analyst access is required")
    engine = getattr(request.app.state, "replay_engine", None)
    if engine is None:
        return Failure("Unavailable", "Restricted replay storage is not configured")
    return ReplayRepository(engine)


@router.get("/simulation-runs", operation_id="c12ListRuns")
def list_runs(
    request: Request,
    context: Identity,
    provider_id: Annotated[UUID4, Query(alias="providerId")],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    scenario_id: Annotated[
        str | None, Query(alias="scenarioId", min_length=1, max_length=200)
    ] = None,
    status: Literal["recording", "completed", "incomplete"] | None = None,
    start: UtcDatetime | None = None,
    end: UtcDatetime | None = None,
):
    repo = repository(request, context)
    if isinstance(repo, Failure):
        return outcome(request, repo)
    if provider_id != context.acting_provider_id:
        return outcome(
            request, Failure("Forbidden", "Requested provider is not authorised")
        )
    if (start is None) != (end is None) or (start and start >= end):
        return outcome(
            request, Failure("Invalid", "Supply an ordered UTC start/end pair")
        )
    filters = {
        "scenarioId": scenario_id,
        "status": status,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }
    return outcome(request, repo.list_runs(context, filters, limit, cursor))


@router.post(
    "/replay-sessions",
    response_model=ReplayFrame | CannotReplay,
    status_code=201,
    operation_id="c12StartReplay",
)
def start_replay(
    body: ReplayRequest,
    request: Request,
    response: Response,
    context: Identity,
    key: Key,
):
    repo = repository(request, context)
    if isinstance(repo, Failure):
        return outcome(request, repo)
    result = repo.start(context, body, key)
    if not isinstance(result, Failure):
        if isinstance(result.value, CannotReplay) or repo.replayed:
            response.status_code = 200
        if isinstance(result.value, ReplayFrame):
            response.headers["Location"] = (
                f"/api/v1/replay-sessions/{result.value.session_id}"
            )
    return outcome(request, result)


@router.get(
    "/replay-sessions/{session_id}",
    response_model=ReplayFrame,
    operation_id="c12GetReplayFrame",
)
def get_frame(session_id: UUID4, request: Request, context: Identity):
    repo = repository(request, context)
    return outcome(
        request,
        repo if isinstance(repo, Failure) else repo.operate(context, session_id),
    )


@router.post(
    "/replay-sessions/{session_id}/controls",
    response_model=ReplayFrame,
    operation_id="c12ControlReplay",
)
def control(
    body: ControlBody, session_id: UUID4, request: Request, context: Identity, key: Key
):
    repo = repository(request, context)
    if isinstance(repo, Failure):
        return outcome(request, repo)
    command = ReplayControl(session_id=session_id, **body.model_dump())
    return outcome(request, repo.operate(context, session_id, command=command, key=key))


@router.post(
    "/replay-sessions/{session_id}/advance",
    response_model=ReplayFrame,
    operation_id="c12AdvanceReplay",
)
def advance(body: AdvanceBody, session_id: UUID4, request: Request, context: Identity):
    """One explicit timeline tick. GET polling never mutates a session."""
    repo = repository(request, context)
    return outcome(
        request,
        repo
        if isinstance(repo, Failure)
        else repo.operate(context, session_id, tick=body.expected_cursor),
    )
