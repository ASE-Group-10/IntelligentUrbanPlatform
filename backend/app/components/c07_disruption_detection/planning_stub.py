"""STUB of C05 Planning for the walking skeleton; replace with the real C05 adapter.

Labelled stand-in so the C06 → C07 → C05 hop runs end to end before C05 exists
(#59, #72). It knows one demo plan that uses the demo segment, taken from the
contract examples in ``contracts/components/c05-planning/examples/``, answers
``requestReplan`` with ``PlanSaved`` and is idempotent on the request key. Like the
real port, it returns the shared ``Result``.
"""

from uuid import UUID, uuid4

from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    AffectedWorkItem,
    ChangeScope,
    ReplanOutcome,
    ReplanRequest,
)
from app.shared.result import Ok, Result

DEMO_SEGMENT = "seg:4471023"
DEMO_PLAN_ID = UUID("9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c")
DEMO_PLAN_VERSION = 3
DEMO_DELIVERY_ID = UUID("7c6b5a49-3827-4165-a0f9-e8d7c6b5a493")


class DemoPlanningStub:
    """Implements ``PlanningPort`` with one fixed demo plan."""

    def __init__(self) -> None:
        self.replan_calls: list[tuple[ReplanRequest, UUID]] = []
        self._outcomes: dict[str, ReplanOutcome] = {}

    def find_affected_work(self, scope: ChangeScope) -> Result[AffectedWork]:
        if DEMO_SEGMENT not in scope.segment_keys:
            return Ok(AffectedWork(items=[]))
        return Ok(
            AffectedWork(
                items=[
                    AffectedWorkItem(
                        plan_id=DEMO_PLAN_ID,
                        plan_version=DEMO_PLAN_VERSION,
                        delivery_ids=[DEMO_DELIVERY_ID],
                        matched_segment_keys=[DEMO_SEGMENT],
                    )
                ]
            )
        )

    def request_replan(
        self, request: ReplanRequest, correlation_id: UUID
    ) -> Result[ReplanOutcome]:
        if request.key in self._outcomes:
            return Ok(self._outcomes[request.key])
        self.replan_calls.append((request, correlation_id))
        outcome = ReplanOutcome(
            status="PlanSaved",
            new_plan_version=request.trigger.based_on_plan_version + 1,
            decision_id=uuid4(),
        )
        self._outcomes[request.key] = outcome
        return Ok(outcome)
