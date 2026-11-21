"""Logical C08 service contracts, not necessarily HTTP endpoints."""

from typing import Protocol
from uuid import UUID

from .events import OperationalDisruptionPayload, PlanReadyEvent
from .models import (
    AllocationOutcome,
    AllocationProblem,
    AssignVehicleCommand,
    AuthContext,
    CommitAssignmentCommand,
    CommitNegotiationCommand,
    IdempotencyKey,
    OfferResponseCommand,
)


class C08Service(Protocol):
    """C08 service boundary.

    Implementations must enforce authentication, idempotency,
    and optimistic concurrency version guards.
    """

    async def handle_plan_ready(self, event: PlanReadyEvent) -> AllocationOutcome:
        """Deduplicate PlanReady and coordinate only a still-current saved version."""
        ...

    async def negotiate_allocation(
        self, context: AuthContext, problem: AllocationProblem, key: IdempotencyKey
    ) -> AllocationOutcome:
        """Run one bounded proposal round, without UC04 driver acceptance."""
        ...

    async def commit_negotiation(
        self, command: CommitNegotiationCommand
    ) -> AllocationOutcome:
        """Atomically check versions/capacity and commit or emit failure outbox."""
        ...

    async def assign_vehicle(self, command: AssignVehicleCommand) -> AllocationOutcome:
        """Prepare UC04 driver offer using an existing commitment when present."""
        ...

    async def record_offer_response(
        self, command: OfferResponseCommand
    ) -> AllocationOutcome:
        """Authoritatively record the driver's UC04 acceptance or rejection."""
        ...

    async def commit_assignment(
        self, command: CommitAssignmentCommand
    ) -> AllocationOutcome:
        """Finalise accepted assignmentRef without taking capacity twice."""
        ...

    async def publish_operational_disruption(
        self,
        context: AuthContext,
        failure: OperationalDisruptionPayload,
        key: IdempotencyKey,
    ) -> UUID:
        """Atomically persist failure and pending disruption outbox entry."""
        ...
