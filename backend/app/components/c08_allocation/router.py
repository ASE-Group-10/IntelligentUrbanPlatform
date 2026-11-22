from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response

from app.integrations.identity import authenticate
from app.shared.auth import AuthContext
from app.shared.contracts import ContractModel

from .models import AllocationOutcome, DriverResponse, OfferResponseCommand
from .offer_response import record_offer_response

OFFER_RESPONSE_PATH = "/api/v1/driver-offers/{offerId}/response"
router = APIRouter(tags=["C08 Allocation and Resource Agents"])


class OfferResponseRequest(ContractModel):
    response: DriverResponse


@router.post(
    OFFER_RESPONSE_PATH,
    operation_id="recordOfferResponse",
    response_model=AllocationOutcome,
    response_model_exclude_none=True,
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Caller cannot respond to this offer"},
        409: {"description": "Offer closed or idempotency key reused"},
        503: {"description": "Database or identity service unavailable"},
    },
)
def respond_to_offer(
    offer_id: Annotated[UUID, Path(alias="offerId")],
    body: OfferResponseRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(authenticate)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ],
) -> AllocationOutcome:
    outcome, replayed = record_offer_response(
        request.app.state.engine,
        context,
        OfferResponseCommand(
            offer_id=offer_id, response=body.response, key=idempotency_key
        ),
        telemetry=request.app.state.c08_telemetry,
    )
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return outcome
