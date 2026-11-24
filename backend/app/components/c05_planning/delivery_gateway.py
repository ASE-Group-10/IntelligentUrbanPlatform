"""Connect a committed C04 delivery to C05's in-process proposal generator."""

from dataclasses import dataclass
from uuid import UUID

from app.components.c04_delivery_management.contracts import Delivery
from app.components.c04_delivery_management.planning import PlanSummary
from app.shared.contracts import RunScopeSelector
from app.shared.result import Failure, Ok, Result

from .models import PlanScope, SavedDelivery
from .service import PlanningService


@dataclass
class DeliveryPlanningGateway:
    planning: PlanningService

    def generate_plan(
        self,
        delivery: Delivery,
        run_scope: RunScopeSelector,
        correlation_id: UUID,
        key: str,
    ) -> Result[PlanSummary]:
        result = self.planning.generate_plan(
            SavedDelivery.model_validate(delivery.to_wire()),
            PlanScope(scope=run_scope.scope, run_id=run_scope.run_id),
            correlation_id,
            key,
        )
        if isinstance(result, Failure):
            return result
        plan = result.value
        return Ok(
            PlanSummary(plan_id=plan.plan_id, version=plan.version, status=plan.status)
        )
