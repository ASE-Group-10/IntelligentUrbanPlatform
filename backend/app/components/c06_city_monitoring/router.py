"""UC05 ``getCitySnapshot`` over HTTP for C01 (see c06-city-monitoring/openapi.yaml).

Query parameters are checked here rather than by FastAPI so that bad input gets
the contract's ``400 Invalid`` instead of the app-wide ``422``.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field, ValidationError
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

router = APIRouter(prefix="/api/v1/city-state", tags=["C06 City Monitoring"])
logger = logging.getLogger("ulop.c06.api")

_STATUS = {"Invalid": 400, "Forbidden": 403, "NotFound": 404, "Unavailable": 503}


class _SnapshotParams(ContractModel):
    segment_keys: list[SegmentKey] = Field(min_length=1)
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
    responses={code: {"description": kind} for kind, code in _STATUS.items()},
)
def get_city_snapshot(
    request: Request,
    _: Annotated[CurrentIdentity, Depends(current_identity)],
    segmentKeys: str | None = None,  # noqa: N803 - contract parameter names
    maxAgeSeconds: str | None = None,  # noqa: N803
    atVersion: str | None = None,  # noqa: N803
    runId: str | None = None,  # noqa: N803
) -> CitySnapshot:
    """Conditions of the requested segments; any signed-in user may read them."""
    try:
        params = _SnapshotParams.model_validate(
            {
                "segmentKeys": segmentKeys.split(",") if segmentKeys else [],
                "maxAgeSeconds": maxAgeSeconds,
                "atVersion": atVersion,
                "runId": runId,
            }
        )
    except ValidationError as error:
        detail = error.errors(include_url=False, include_input=False)[0]
        field = ".".join(map(str, detail["loc"]))
        raise APIError(400, "Invalid", f"{field}: {detail['msg']}") from error

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
