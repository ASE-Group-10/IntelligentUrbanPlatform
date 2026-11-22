"""Persist the authenticated driver's answer to one stored UC04 offer."""

from contextlib import nullcontext
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Engine, RowMapping

from app.infrastructure.errors import APIError
from app.shared.auth import AuthContext

from .models import (
    AcceptanceStatus,
    AllocationOutcome,
    DriverResponse,
    OfferResponseCommand,
    OutcomeCode,
)

if TYPE_CHECKING:
    from .telemetry import C08Telemetry


def record_offer_response(
    engine: Engine,
    context: AuthContext,
    command: OfferResponseCommand,
    *,
    telemetry: "C08Telemetry | None" = None,
) -> tuple[AllocationOutcome, bool]:
    if (
        context.principal_type != "user"
        or context.acting_provider_id is None
        or "driver" not in context.roles
    ):
        raise APIError(403, "FORBIDDEN", "Only the intended driver may respond.")

    params = {
        "offer_id": command.offer_id,
        "provider_id": context.acting_provider_id,
        "driver_id": context.principal_id,
        "response_key": command.key,
        "response": command.response.value,
        "correlation_id": context.correlation_id,
    }
    observation = telemetry.transaction() if telemetry else nullcontext()
    with observation, engine.begin() as connection:
        # Serialize retries even if two requests using this key target different offers.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {
                "scope": f"c08.offer-response:{context.acting_provider_id}:{context.principal_id}:{command.key}"
            },
        )
        receipt = (
            connection.execute(
                text("""
                SELECT offer_id, assignment_ref, response, response_correlation_id
                FROM c08_allocation.driver_offers
                WHERE provider_id = :provider_id AND driver_id = :driver_id
                    AND response_key = :response_key
            """),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if receipt is not None:
            if (
                receipt["offer_id"] != command.offer_id
                or receipt["response"] != command.response
            ):
                raise APIError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This key was used for a different response.",
                )
            return _outcome(receipt), True

        offer = connection.execute(
            text("""
                SELECT offer_id
                FROM c08_allocation.driver_offers
                WHERE offer_id = :offer_id AND provider_id = :provider_id AND driver_id = :driver_id
                FOR UPDATE
            """),
            params,
        ).scalar_one_or_none()
        if offer is None:
            raise APIError(403, "FORBIDDEN", "Only the intended driver may respond.")

        updated = (
            connection.execute(
                text("""
                UPDATE c08_allocation.driver_offers
                SET response = :response, response_key = :response_key,
                    response_correlation_id = :correlation_id, responded_at = statement_timestamp()
                WHERE offer_id = :offer_id AND response IS NULL
                    AND expires_at > statement_timestamp()
                RETURNING assignment_ref, response, response_correlation_id
            """),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise APIError(
                409, "OFFER_CLOSED", "The offer expired or already has a response."
            )
        return _outcome(updated), False


def _outcome(row: RowMapping) -> AllocationOutcome:
    accepted = row["response"] == DriverResponse.ACCEPT
    return AllocationOutcome(
        code=OutcomeCode.CONFIRMED if accepted else OutcomeCode.UNASSIGNED,
        correlation_id=row["response_correlation_id"],
        assignment_ref=row["assignment_ref"],
        acceptance_status=AcceptanceStatus.ACCEPTED
        if accepted
        else AcceptanceStatus.REJECTED,
    )
