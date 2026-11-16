"""Port from C07 to C05 Planning: the two synchronous calls C07 makes.

C07 depends on this interface only. The real C05 adapter replaces the test
double once C05 exists (see contracts/c06-c07-interface-contracts.md, section 3).
"""

from typing import Protocol
from uuid import UUID

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ChangeScope,
    ReplanOutcome,
    ReplanRequest,
)


class PlanningUnavailableError(Exception):
    """C05 could not answer the call (contract failure kind ``Unavailable``)."""


class PlanningPort(Protocol):
    """What C07 needs from C05 Planning."""

    def find_affected_work(self, scope: ChangeScope) -> AffectedWork:
        """Return the unfinished work that ``scope`` touches (empty items = none).

        Raises:
            PlanningUnavailableError: if C05 cannot answer; C07 then reports the
                disruption status as ``unknown``.
        """
        ...

    def request_replan(
        self, request: ReplanRequest, correlation_id: UUID
    ) -> ReplanOutcome:
        """Ask C05 to revise one plan and return its final outcome.

        The call is idempotent on ``request.key``: repeating it returns the first
        outcome again instead of replanning twice.

        Raises:
            PlanningUnavailableError: if C05 cannot answer.
        """
        ...
