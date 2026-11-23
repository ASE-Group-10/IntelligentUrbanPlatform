"""TEST DOUBLE of C05 Planning for the C07 tests; not production code.

The real C05 adapter replaces it once C05 exists. It answers ``findAffectedWork``
from a fixed table of "which plans use which segment" and ``requestReplan`` with a
fixed outcome, and returns the shared ``Result`` like the real port. Tests can make
it fail:

- ``unavailable`` / ``find_failure``: ``findAffectedWork`` returns a ``Failure``.
- ``replan_unavailable``: every ``requestReplan`` returns ``Failure("Unavailable")``.
- ``plan_failures``: plan id -> ``Failure`` returned on every ``requestReplan`` for
  that plan until the entry is removed (for example a plan C05 never finds).
- ``replan_failures``: a queue of ``Failure`` values, one returned per ``requestReplan``
  call before ``outcome`` is used (for example ``[Failure("Conflict", ...)]``).
- ``work_after_conflict``: once a ``Conflict`` is returned, the plans move on and
  ``findAffectedWork`` answers from this table instead (a newer ``planVersion``, or
  an empty table when the plan is no longer affected).
- ``down_after_conflict``: once a ``Conflict`` is returned, C05 goes down
  (``unavailable`` becomes true), so the rediscovery answers ``Unavailable``.

It records every call it receives (``find_calls``, ``replan_calls``).
"""

from uuid import UUID

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ChangeScope,
    ReplanOutcome,
    ReplanRequest,
)
from app.shared.result import Failure, Ok, Result


class FakePlanning:
    """In-memory stand-in for C05 (implements ``PlanningPort``)."""

    def __init__(
        self,
        work_by_segment: dict[str, AffectedWork] | None = None,
        outcome: ReplanOutcome | None = None,
        unavailable: bool = False,
        replan_unavailable: bool = False,
        find_failure: Failure | None = None,
        replan_failures: list[Failure] | None = None,
        plan_failures: dict[UUID, Failure] | None = None,
        work_after_conflict: dict[str, AffectedWork] | None = None,
        down_after_conflict: bool = False,
    ) -> None:
        self._work_by_segment = work_by_segment or {}
        self._outcome = outcome or ReplanOutcome(status="NoChange")
        self._work_after_conflict = work_after_conflict
        self._down_after_conflict = down_after_conflict
        self.unavailable = unavailable
        self.replan_unavailable = replan_unavailable
        self.find_failure = find_failure
        self.replan_failures = list(replan_failures or [])
        self.plan_failures = dict(plan_failures or {})
        self.find_calls: list[ChangeScope] = []
        self.replan_calls: list[tuple[ReplanRequest, UUID]] = []

    def find_affected_work(self, scope: ChangeScope) -> Result[AffectedWork]:
        self.find_calls.append(scope)
        if self.unavailable:
            return Failure("Unavailable", "C05 is not reachable")
        if self.find_failure is not None:
            return self.find_failure
        items = []
        for key in scope.segment_keys:
            for item in self._work_by_segment.get(key, AffectedWork(items=[])).items:
                if all(known.plan_id != item.plan_id for known in items):
                    items.append(item)
        return Ok(AffectedWork(items=items))

    def request_replan(
        self, request: ReplanRequest, correlation_id: UUID
    ) -> Result[ReplanOutcome]:
        self.replan_calls.append((request, correlation_id))
        if self.replan_unavailable:
            return Failure("Unavailable", "C05 is not reachable")
        if request.plan_id in self.plan_failures:
            return self.plan_failures[request.plan_id]
        if self.replan_failures:
            failure = self.replan_failures.pop(0)
            if failure.kind == "Conflict":
                if self._work_after_conflict is not None:
                    self._work_by_segment = self._work_after_conflict
                self.unavailable = self._down_after_conflict
            return failure
        return Ok(self._outcome)
