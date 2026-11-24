"""Save a confirmed plan version and its PlanReady envelope together."""

from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.components.c08_allocation.events import PlanReadyEvent
from app.components.c08_allocation.models import PlanReadyPayload
from app.shared.contracts import Affected

from .models import Plan
from .store import scope_key


def save_ready_version(
    conn: psycopg.Connection,
    plan: Plan,
    departure: datetime,
    correlation_id: UUID,
    now: datetime,
    *,
    causation_id: UUID | None = None,
) -> UUID:
    """Save both rows on the caller's locked transaction for later publication."""
    segments = dict.fromkeys(key for route in plan.routes for key in route.segment_keys)
    event = PlanReadyEvent(
        event_id=uuid4(),
        event_type="PlanReady",
        producer="C05",
        schema_version=1,
        observed_at=now,
        emitted_at=now,
        correlation_id=correlation_id,
        causation_id=causation_id,
        scope=plan.run_scope.scope,
        run_id=plan.run_scope.run_id,
        affected=Affected(
            plan_ids=[plan.plan_id], resource_ids=[], segment_keys=list(segments)
        ),
        entity_versions={str(plan.plan_id): plan.version},
        payload=PlanReadyPayload(
            plan_id=plan.plan_id,
            plan_version=plan.version,
            decision_id=plan.decision_id,
            requirements=plan.resource_needs,
        ),
    )
    conn.execute(
        """insert into c05_planning.plan_versions
                  (proposal, correlation_id, planned_departure_at)
           values (%s,%s,%s)""",
        (Jsonb(plan.model_dump(mode="json", by_alias=True)), correlation_id, departure),
    )
    conn.execute(
        """insert into c05_planning.outbox
                  (event_id, scope_key, plan_id, version, event_type, envelope)
           values (%s,%s,%s,%s,%s,%s)""",
        (
            event.event_id,
            scope_key(plan.run_scope.scope, plan.run_scope.run_id),
            plan.plan_id,
            plan.version,
            event.event_type,
            Jsonb(event.model_dump(mode="json", by_alias=True, exclude_none=True)),
        ),
    )
    return event.event_id
