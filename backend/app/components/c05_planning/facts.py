"""Find confirmed plans whose unfinished deliveries use a changed route."""

from datetime import datetime
from uuid import UUID

import psycopg
from pydantic import ValidationError

from app.components.c04_delivery_management import planning as deliveries
from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    AffectedWorkItem,
    ChangeScope,
)
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure, Ok, Result

from .models import Plan, Route, SavedDelivery
from .store import PostgresPlanStore, scope_key


def linked_deliveries(
    conn: psycopg.Connection, plan: Plan, correlation_id: UUID
) -> Result[list[SavedDelivery]]:
    """Read the current versions and progress through C04, including completed work."""
    ctx = AuthContext(
        principal_id="service:c05",
        principal_type="Service",
        roles=["Service:C05"],
        correlation_id=correlation_id,
    )
    rows = deliveries.get_requests(
        conn, ctx, plan.run_scope, plan.request_ids, linked_to_plan=plan.plan_id
    )
    if isinstance(rows, Failure):
        # A missing authoritative input cannot establish that work is finished.
        return Failure("Unavailable", rows.message)
    try:
        return Ok([SavedDelivery.model_validate(row) for row in rows.value])
    except ValidationError:
        return Failure("Unavailable", "C05.INVALID_SAVED_DELIVERY")


def is_unfinished(delivery: SavedDelivery) -> bool:
    return (
        delivery.status in ("planned", "in_progress")
        and delivery.progress.status != "delivered"
    )


def _affected_item(
    conn: psycopg.Connection,
    scope: ChangeScope,
    plan: Plan,
    departure: datetime,
    correlation_id: UUID,
) -> Result[AffectedWorkItem | None]:
    named_plan = plan.plan_id in scope.plan_ids
    changed = set(scope.segment_keys)

    def touches(route: Route) -> bool:
        if scope.from_ is not None:
            elapsed = (scope.from_ - departure).total_seconds()
            if elapsed >= route.estimated_duration_seconds:
                return False
        return named_plan or not changed.isdisjoint(route.segment_keys)

    candidates = [route for route in plan.routes if touches(route)]
    if not candidates:
        return Ok(None)
    facts = linked_deliveries(conn, plan, correlation_id)
    if isinstance(facts, Failure):
        return facts
    unfinished = {d.delivery_id for d in facts.value if is_unfinished(d)}
    matched_ids: set[UUID] = set()
    matched_keys: set[str] = set()
    for route in candidates:
        on_route = unfinished.intersection(route.delivery_ids)
        if on_route:
            matched_ids.update(on_route)
            matched_keys.update(route.segment_keys)
    if not matched_ids:
        return Ok(None)
    return Ok(
        AffectedWorkItem(
            plan_id=plan.plan_id,
            plan_version=plan.version,
            delivery_ids=[did for did in plan.request_ids if did in matched_ids],
            matched_segment_keys=[
                s for s in dict.fromkeys(scope.segment_keys) if s in matched_keys
            ],
            matched_resource_ids=[],
        )
    )


def find_affected_work(
    store: PostgresPlanStore, scope: ChangeScope
) -> Result[AffectedWork]:
    """Find work on current plan versions in the event partition for C07."""
    if scope.from_ and scope.to and scope.from_ >= scope.to:
        return Failure("Invalid", "C05.INVALID_CHANGE_WINDOW")
    if scope.resource_ids:
        # Allocation bindings belong to C08. The Dublin slice handles road changes.
        return Failure("Invalid", "C05.RESOURCE_IMPACT_NOT_SUPPORTED")
    try:
        with store.transaction() as conn:
            heads = conn.execute(
                """select proposal, correlation_id, planned_departure_at
                     from (select distinct on (plan_id)
                                  plan_id, proposal, correlation_id,
                                  planned_departure_at
                             from c05_planning.plan_versions
                            where scope_key = %s order by plan_id, version desc) heads
                    where proposal->>'status' = 'confirmed'
                    order by plan_id""",
                (scope_key(scope.scope, scope.run_id),),
            ).fetchall()
            items = []
            for proposal, trace, departure in heads:
                if scope.to is not None and departure >= scope.to:
                    continue
                plan = Plan.model_validate(proposal, strict=False)
                result = _affected_item(conn, scope, plan, departure, trace)
                if isinstance(result, Failure):
                    return result
                if result.value is not None:
                    items.append(result.value)
    except (psycopg.Error, ValidationError):
        return Failure("Unavailable", "C05.PLANNING_FACTS_UNAVAILABLE")
    return Ok(AffectedWork(items=items))
