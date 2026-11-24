"""Read C05 plan versions on C08's PostgreSQL allocation transaction."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

from app.shared.contracts import RunScopeSelector
from app.shared.result import Failure, Ok, Result

from .models import Plan, Route
from .store import plan_lock_key, scope_key


@dataclass(frozen=True)
class ConfirmedDeliveryRoute:
    plan: Plan
    route: Route
    planned_departure_at: datetime


def get_delivery_route(
    conn: Connection,
    run_scope: RunScopeSelector,
    plan_id: UUID,
    plan_version: int,
    delivery_id: UUID,
) -> Result[ConfirmedDeliveryRoute]:
    """Read the delivery's route and departure from one immutable confirmed version."""
    row = conn.execute(
        text("""
            select proposal, planned_departure_at from c05_planning.plan_versions
             where scope_key = :scope and plan_id = :plan and version = :version
               and proposal ->> 'status' = 'confirmed'
        """),
        {
            "scope": scope_key(run_scope.scope, run_scope.run_id),
            "plan": plan_id,
            "version": plan_version,
        },
    ).one_or_none()
    if row is None:
        return Failure("NotFound", "C05.CONFIRMED_VERSION_NOT_FOUND")
    plan = Plan.model_validate(row.proposal, strict=False)
    routes = [route for route in plan.routes if delivery_id in route.delivery_ids]
    if delivery_id not in plan.request_ids or not routes:
        return Failure("NotFound", "C05.DELIVERY_ROUTE_NOT_FOUND")
    if len(routes) != 1:
        return Failure("Invalid", "C05.AMBIGUOUS_DELIVERY_ROUTE")
    return Ok(ConfirmedDeliveryRoute(plan, routes[0], row.planned_departure_at))


def check_current_version(
    conn: Connection,
    run_scope: RunScopeSelector,
    plan_id: UUID,
    plan_version: int,
) -> Result[None]:
    """Keep the plan current through the caller's READ COMMITTED transaction."""
    # SQLAlchemy also permits begin() in DBAPI autocommit mode, where PostgreSQL
    # would release the advisory lock as soon as the SELECT finishes.
    if not conn.in_transaction() or conn.connection.driver_connection.autocommit:
        return Failure("Unavailable", "C05.CURRENT_CHECK_REQUIRES_TRANSACTION")
    # Take the lock in a separate statement. If a writer was already revising the
    # plan, the next READ COMMITTED snapshot then sees its newly committed version.
    lock = conn.execute(
        text("""
            select pg_advisory_xact_lock_shared(hashtextextended(:lock_key, 0))
             where current_setting('transaction_isolation') = 'read committed'
        """),
        {"lock_key": plan_lock_key(run_scope, plan_id)},
    ).one_or_none()
    if lock is None:
        return Failure("Unavailable", "C05.CURRENT_CHECK_REQUIRES_READ_COMMITTED")
    row = conn.execute(
        text("""
            select version, proposal ->> 'status' as status
              from c05_planning.plan_versions
             where scope_key = :scope and plan_id = :plan
             order by version desc limit 1
        """),
        {"scope": scope_key(run_scope.scope, run_scope.run_id), "plan": plan_id},
    ).one_or_none()
    if row is None:
        return Failure("NotFound", "C05.PLAN_NOT_FOUND")
    if row.version != plan_version or row.status != "confirmed":
        return Failure("Conflict", "C05.PLAN_VERSION_CHANGED")
    return Ok(None)
