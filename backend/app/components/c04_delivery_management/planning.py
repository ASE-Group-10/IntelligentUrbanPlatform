"""In-process C04 → C05 planning handoff boundary for the walking skeleton (#72).

C04 invokes ``PlanningGateway.generate_plan`` after durably committing a new
delivery request. Until C05's implementation (#118/#119) lands on ``develop``,
``StubPlanningGateway`` acts as a clearly labelled stub that logs the original
``correlationId`` and returns a deterministic stub plan reference.
"""

import logging
from typing import Literal, Protocol
from uuid import UUID, uuid5

from app.components.c04_delivery_management.contracts import Delivery, scope_key_for
from app.shared.contracts import ContractModel, RunScopeSelector, Version
from app.shared.result import Ok, Result

log = logging.getLogger("ulop.c04.planning")

_STUB_PLAN_NAMESPACE = UUID("8b7c6d5e-4f3a-4b2c-9d1e-0f1a2b3c4d5e")


class PlanSummary(ContractModel):
    """Compact summary returned by C05 ``generatePlan`` to C04."""

    plan_id: UUID
    version: Version
    status: Literal["awaiting_confirmation", "confirmed"]
    stub: bool = False


class PlanningGateway(Protocol):
    """In-process C05 planning handoff required by C04 after delivery commit."""

    def generate_plan(
        self,
        delivery: Delivery,
        run_scope: RunScopeSelector,
        correlation_id: UUID,
        key: str,
    ) -> Result[PlanSummary]: ...


class StubPlanningGateway:
    """Labelled walking-skeleton stub for C05 ``generatePlan`` until #118 merges."""

    def generate_plan(
        self,
        delivery: Delivery,
        run_scope: RunScopeSelector,
        correlation_id: UUID,
        key: str,
    ) -> Result[PlanSummary]:
        plan_id = uuid5(
            _STUB_PLAN_NAMESPACE,
            f"{scope_key_for(run_scope)}:{delivery.delivery_id}:v{delivery.version}",
        )
        summary = PlanSummary(
            plan_id=plan_id,
            version=1,
            status="awaiting_confirmation",
            stub=True,
        )
        log.info(
            "generatePlan (stub): plan %s v%d for delivery %s (replace with C05 #118)",
            summary.plan_id,
            summary.version,
            delivery.delivery_id,
            extra={
                "component": "C05",
                "correlationId": str(correlation_id),
                "scope": run_scope.scope,
                "runId": str(run_scope.run_id) if run_scope.run_id else None,
            },
        )
        return Ok(summary)
