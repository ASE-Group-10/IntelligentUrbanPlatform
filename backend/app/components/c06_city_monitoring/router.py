"""UC05 ``getCitySnapshot`` over HTTP for C01 (see c06-city-monitoring/openapi.yaml).

Query parameters are checked here rather than by FastAPI so that bad input gets
the contract's ``400 Invalid`` instead of the app-wide ``422``. Because FastAPI
does not see them, the operation documents them itself (``_PARAMETERS``) so
``/openapi.json``, which C01 generates its client from, matches the contract.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import make_url

from app.components.c06_city_monitoring.contracts import (
    CitySnapshot,
    CitySnapshotQuery,
    FreshnessPolicy,
    SnapshotScope,
)
from app.components.c06_city_monitoring.store import StateScope
from app.infrastructure.errors import APIError, correlation_id
from app.integrations.identity import current_identity
from app.shared.auth import CurrentIdentity
from app.shared.contracts import ContractModel, SegmentKey, Version
from app.shared.result import Failure
from app.shared.run_access import require_run_access

router = APIRouter(prefix="/api/v1/city-state", tags=["C06 City Monitoring"])
logger = logging.getLogger("ulop.c06.api")

_STATUS = {"Invalid": 400, "Forbidden": 403, "NotFound": 404, "Unavailable": 503}

_PARAMETERS = [
    {
        "name": "segmentKeys",
        "in": "query",
        "required": True,
        "description": "Comma-separated segment keys to read.",
        "style": "form",
        "explode": False,
        "schema": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "items": {"type": "string", "pattern": "^seg:[1-9][0-9]*$"},
        },
    },
    {
        "name": "maxAgeSeconds",
        "in": "query",
        "required": False,
        "description": "Values older than this are returned as `degraded`.",
        "schema": {"type": "integer", "minimum": 1, "default": 900},
    },
    {
        "name": "atVersion",
        "in": "query",
        "required": False,
        "description": "Read City State as it was at this version (C10, C12).",
        "schema": {"type": "integer", "minimum": 1},
    },
    {
        "name": "runId",
        "in": "query",
        "required": False,
        "description": "Simulation run to read. Leave out to read live City State.",
        "schema": {"type": "string", "format": "uuid"},
    },
]


class _ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: UUID


class C06ErrorResponse(BaseModel):
    error: _ErrorBody


_RESPONSES = {
    400: "Missing or malformed segment keys or parameters.",
    401: "Missing, invalid or expired bearer token.",
    403: "The caller may not read this City State or simulation run.",
    404: "`atVersion` is newer than the current City State version.",
    503: "City State, or C02 sign-in, is unavailable.",
}


class _SnapshotParams(ContractModel):
    segment_keys: list[SegmentKey] = Field(min_length=1, max_length=200)
    max_age_seconds: Annotated[int, Field(ge=1)] | None = None
    at_version: Version | None = None
    run_id: UUID | None = None


def libpq_url(url: str) -> str:
    """Turn the API's SQLAlchemy ``DATABASE_URL`` into one psycopg accepts."""
    return (
        make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


@router.get(
    "/snapshot",
    response_model=CitySnapshot,
    operation_id="getCitySnapshot",
    responses={
        code: {"description": text, "model": C06ErrorResponse}
        for code, text in _RESPONSES.items()
    },
    openapi_extra={"parameters": _PARAMETERS},
)
def get_city_snapshot(
    request: Request,
    _: Annotated[CurrentIdentity, Depends(current_identity)],
) -> CitySnapshot:
    """Conditions of the requested segments. Live state is open to every
    signed-in user; a simulation run only when it is a shared run."""
    query_params = request.query_params
    segment_keys = query_params.get("segmentKeys")
    try:
        params = _SnapshotParams.model_validate(
            {
                "segmentKeys": segment_keys.split(",") if segment_keys else [],
                "maxAgeSeconds": query_params.get("maxAgeSeconds"),
                "atVersion": query_params.get("atVersion"),
                "runId": query_params.get("runId"),
            }
        )
    except ValidationError as error:
        detail = error.errors(include_url=False, include_input=False)[0]
        field = ".".join(map(str, detail["loc"]))
        raise APIError(400, "Invalid", f"{field}: {detail['msg']}") from error

    require_run_access(request, params.run_id)

    scope = (
        StateScope(scope="simulation", run_id=params.run_id)
        if params.run_id
        else StateScope(scope="live")
    )
    query = CitySnapshotQuery(
        scope=SnapshotScope(segment_keys=params.segment_keys),
        freshness_policy=FreshnessPolicy(max_age_seconds=params.max_age_seconds),
        at_version=params.at_version,
    )
    result = request.app.state.city_monitoring.get_city_snapshot(scope, query)
    if isinstance(result, Failure):
        message = result.message
        if result.kind == "Unavailable":
            logger.warning(
                "City State read failed correlation_id=%s reason=%s",
                correlation_id(request),
                message,
            )
            message = "City State is unavailable. Try again shortly."
        raise APIError(_STATUS[result.kind], result.kind, message)
    return result.value
