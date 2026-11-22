"""UC06 ``listDisruptions`` over HTTP for C01 (c07-disruption-detection/openapi.yaml).

``runId`` is checked here rather than by FastAPI so that a malformed one gets
the contract's ``400 Invalid`` instead of the app-wide ``422``. Because FastAPI
does not see it, the operation documents it itself so ``/openapi.json``, which
C01 generates its client from, matches the contract.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ValidationError

from app.components.c07_disruption_detection.contracts import Disruption
from app.components.c07_disruption_detection.store import (
    DisruptionStoreUnavailableError,
)
from app.infrastructure.errors import APIError, correlation_id
from app.integrations.identity import current_identity
from app.shared.auth import CurrentIdentity
from app.shared.contracts import ContractModel
from app.shared.run_access import require_run_access

router = APIRouter(prefix="/api/v1/disruptions", tags=["C07 Disruption Detection"])
logger = logging.getLogger("ulop.c07.api")

_PARAMETERS = [
    {
        "name": "runId",
        "in": "query",
        "required": False,
        "description": "Simulation run to read. Leave out to read live disruptions.",
        "schema": {"type": "string", "format": "uuid"},
    },
]


class DisruptionList(ContractModel):
    disruptions: list[Disruption]


class _ListParams(ContractModel):
    run_id: UUID | None = None


class _ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: UUID


class C07ErrorResponse(BaseModel):
    error: _ErrorBody


_RESPONSES = {
    400: "Malformed `runId`.",
    401: "Missing, invalid or expired bearer token.",
    403: "The caller may not read this simulation run.",
    503: "The disruption store, or C02 sign-in, is unavailable.",
}


@router.get(
    "",
    response_model=DisruptionList,
    operation_id="listDisruptions",
    responses={
        code: {"description": text, "model": C07ErrorResponse}
        for code, text in _RESPONSES.items()
    },
    openapi_extra={"parameters": _PARAMETERS},
)
def list_disruptions(
    request: Request,
    _: Annotated[CurrentIdentity, Depends(current_identity)],
) -> DisruptionList:
    """Disruptions recorded in one scope, most recently updated first."""
    try:
        params = _ListParams.model_validate(
            {"runId": request.query_params.get("runId")}
        )
    except ValidationError as error:
        detail = error.errors(include_url=False, include_input=False)[0]
        raise APIError(400, "Invalid", f"runId: {detail['msg']}") from error
    require_run_access(request, params.run_id)

    scope = "simulation" if params.run_id else "live"
    try:
        found = request.app.state.disruption_store.disruptions(scope, params.run_id)
    except DisruptionStoreUnavailableError as error:
        logger.warning(
            "Disruption read failed correlation_id=%s reason=%s",
            correlation_id(request),
            error,
        )
        raise APIError(
            503, "Unavailable", "Disruptions are unavailable. Try again shortly."
        ) from error
    newest_first = sorted(
        found, key=lambda d: (d.updated_at, str(d.disruption_id)), reverse=True
    )
    return DisruptionList(disruptions=newest_first)
