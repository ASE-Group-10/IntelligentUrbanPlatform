"""UC10 read operations over HTTP for C01 (c10-decision-records/openapi.yaml).

getDecision, findDecision and getDecisionEvidence only; explainDecision is added
separately. Errors use C02's body through ``APIError``.

Two rules are provisional until the C09/C10 pair agrees them (#60):

- C02 role names are translated to the names the service checks (``READ_AS``).
- Records do not say which provider they belong to, so only simulation runs the
  caller may read are served and live decisions are refused. Providers sharing a
  run can read each other's decisions in it.
"""

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Path, Query, Request

from app.components.c10_decision_records.contracts import (
    DecisionEvidence,
    DecisionIdSelector,
    DecisionRecordView,
    SubjectSelector,
    SubjectType,
)
from app.components.c10_decision_records.service import (
    FORBIDDEN,
    UNAVAILABLE,
    AmbiguousDecision,
    DecisionRecords,
)
from app.infrastructure.errors import APIError
from app.integrations.identity import authenticate
from app.shared import auth
from app.shared.c09_c10_common import AuthContext, ErrorResponse, PrincipalType
from app.shared.result import Failure, Result
from app.shared.run_access import require_run_access

READ_AS = {"operator": "LogisticsOperator", "analyst": "Analyst"}

_STATUS = {"Forbidden": 403, "NotFound": 404, "Conflict": 409}
_UNAVAILABLE_TEXT = "Decision records are unavailable. Try again shortly."

router = APIRouter(
    prefix="/api/v1/decisions",
    tags=["C10 Decision Records and Explanation"],
    responses={
        status: {"model": ErrorResponse} for status in (401, 403, 404, 422, 503)
    },
)

RunId = Annotated[UUID | None, Query(alias="runId")]


def _reader(
    request: Request,
    user: Annotated[auth.AuthContext, Depends(authenticate)],
    run_id: RunId = None,
) -> AuthContext:
    """The C02 user as a C10 caller, if they may read decisions in this run."""
    roles = [READ_AS[role] for role in user.roles if role in READ_AS]
    if not roles:
        raise APIError(403, FORBIDDEN, "The caller may not read decisions.")
    if run_id is None:
        raise APIError(
            403, FORBIDDEN, "Live decisions are not available yet; send runId."
        )
    try:
        require_run_access(request, run_id)
    except APIError as error:  # its code is not in this contract's form
        raise APIError(error.status, FORBIDDEN, error.message) from error
    return AuthContext(
        principal_id=str(user.principal_id),
        principal_type=PrincipalType.USER,
        roles=roles,
        correlation_id=user.correlation_id,
    )


Reader = Annotated[AuthContext, Depends(_reader)]


def _read[T](
    request: Request,
    operation: Callable[[DecisionRecords, psycopg.Connection], Result[T]],
) -> T:
    """Run one read on its own connection; raise its failure as an HTTP error."""
    state = request.app.state
    try:
        with state.decision_connection() as conn:
            result = operation(state.decisions, conn)
    except psycopg.OperationalError as error:
        raise APIError(503, UNAVAILABLE, _UNAVAILABLE_TEXT) from error
    if not isinstance(result, Failure):
        return result.value
    code, _, message = result.message.partition(": ")
    if code == UNAVAILABLE:  # the rest names storage internals; it is in the log
        message = _UNAVAILABLE_TEXT
    extra = {}
    if isinstance(result, AmbiguousDecision):
        extra["details"] = {
            "candidates": [
                {"decisionId": str(decision_id), "subjectVersion": version}
                for decision_id, version in result.candidates
            ]
        }
    raise APIError(_STATUS.get(result.kind, 503), code, message, **extra)


@router.get(
    "/{decisionId}", response_model=DecisionRecordView, operation_id="getDecision"
)
def get_decision(
    request: Request,
    reader: Reader,
    decision_id: Annotated[UUID, Path(alias="decisionId")],
    run_id: RunId = None,
) -> DecisionRecordView:
    """The record as stored, with the status of each evidence reference."""
    selector = DecisionIdSelector(decision_id=decision_id, run_id=run_id)
    return _read(request, lambda c10, conn: c10.get_decision(conn, reader, selector))


@router.get(
    "",
    response_model=DecisionRecordView,
    operation_id="findDecision",
    responses={409: {"model": ErrorResponse}},
)
def find_decision(
    request: Request,
    reader: Reader,
    subject_type: Annotated[
        Literal["Plan", "Allocation", "Negotiation"], Query(alias="subjectType")
    ],
    subject_id: Annotated[UUID, Query(alias="subjectId")],
    subject_version: Annotated[int | None, Query(alias="subjectVersion", ge=1)] = None,
    run_id: RunId = None,
) -> DecisionRecordView:
    """The only decision about a subject; several are ``C10.DECISION_AMBIGUOUS``."""
    selector = SubjectSelector(
        subject_type=SubjectType(subject_type),
        subject_id=subject_id,
        subject_version=subject_version,
        run_id=run_id,
    )
    return _read(request, lambda c10, conn: c10.find_decision(conn, reader, selector))


@router.get(
    "/{decisionId}/evidence/{referenceId}",
    response_model=DecisionEvidence,
    operation_id="getDecisionEvidence",
)
def get_decision_evidence(
    request: Request,
    reader: Reader,
    decision_id: Annotated[UUID, Path(alias="decisionId")],
    reference_id: Annotated[str, Path(alias="referenceId")],
    run_id: RunId = None,
) -> DecisionEvidence:
    """One passage or alternative as saved at decision time."""
    selector = DecisionIdSelector(decision_id=decision_id, run_id=run_id)
    return _read(
        request,
        lambda c10, conn: c10.get_decision_evidence(
            conn, reader, selector, reference_id
        ),
    )
