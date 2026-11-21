"""Port from C07 to C05 Planning: the two synchronous calls C07 makes.

C07 depends on this interface only. The real C05 adapter replaces the test
double once C05 exists (see contracts/components/c07-disruption-detection/internal.md).
Both calls return the shared ``Result``: ``Ok(value)`` or ``Failure(kind, message)``.
"""

from typing import Protocol
from uuid import UUID

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ChangeScope,
    ReplanOutcome,
    ReplanRequest,
)
from app.shared.result import Result


class PlanningPort(Protocol):
    """What C07 needs from C05 Planning."""

    def find_affected_work(self, scope: ChangeScope) -> Result[AffectedWork]:
        """Return the unfinished work that ``scope`` touches (empty items = none).

        ``scope.scope`` and ``scope.run_id`` select the run whose plans are searched.

        Failures: ``Invalid`` (bad scope) and ``Unavailable``. C07 reports any
        failure as ``unknown``, and ``Unavailable`` also as retryable.
        """
        ...

    def request_replan(
        self, request: ReplanRequest, correlation_id: UUID
    ) -> Result[ReplanOutcome]:
        """Ask C05 to revise one plan and return its final outcome.

        The call is idempotent on ``request.key``: repeating it returns the first
        outcome again instead of replanning twice. An infeasible revision is not a
        failure: it comes back as ``Ok`` with status ``ReviewRequired`` and a reason.

        Failures: ``Invalid``, ``NotFound``, ``Conflict`` (the plan version moved
        on; C07 treats any ``Conflict`` as a stale plan and retries once) and
        ``Unavailable``.
        """
        ...
