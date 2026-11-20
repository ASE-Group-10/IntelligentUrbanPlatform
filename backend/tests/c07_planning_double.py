"""TEST DOUBLE of C05 Planning for the C07 tests; not production code.

The real C05 adapter replaces it once C05 exists. It answers ``findAffectedWork``
from a fixed table of "which plans use which segment", can be switched to
unavailable (``unavailable`` for ``findAffectedWork``, ``replan_unavailable`` for
``requestReplan``), and records every call it receives so tests can assert on them.
"""

from uuid import UUID

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ChangeScope,
    ReplanOutcome,
    ReplanRequest,
)
from app.components.c07_disruption_detection.planning import PlanningUnavailableError


class FakePlanning:
    """In-memory stand-in for C05 (implements ``PlanningPort``)."""

    def __init__(
        self,
        work_by_segment: dict[str, AffectedWork] | None = None,
        outcome: ReplanOutcome | None = None,
        unavailable: bool = False,
        replan_unavailable: bool = False,
    ) -> None:
        self._work_by_segment = work_by_segment or {}
        self._outcome = outcome or ReplanOutcome(status="NoChange")
        self.unavailable = unavailable
        self.replan_unavailable = replan_unavailable
        self.find_calls: list[ChangeScope] = []
        self.replan_calls: list[tuple[ReplanRequest, UUID]] = []

    def find_affected_work(self, scope: ChangeScope) -> AffectedWork:
        self.find_calls.append(scope)
        if self.unavailable:
            raise PlanningUnavailableError("C05 is not reachable")
        items = [
            item
            for key in scope.segment_keys
            for item in self._work_by_segment.get(key, AffectedWork(items=[])).items
        ]
        return AffectedWork(items=items)

    def request_replan(
        self, request: ReplanRequest, correlation_id: UUID
    ) -> ReplanOutcome:
        self.replan_calls.append((request, correlation_id))
        if self.replan_unavailable:
            raise PlanningUnavailableError("C05 is not reachable")
        return self._outcome
