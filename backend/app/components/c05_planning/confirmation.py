"""Confirm a committed proposal, its C04 delivery links and its first PlanReady."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from app.components.c04_delivery_management import planning as deliveries
from app.components.c10_decision_records.contracts import AuditOutcome
from app.components.c10_decision_records.service import DecisionRecords
from app.shared.auth import AuthContext, require_role
from app.shared.c09_c10_common import AuthContext as ServiceContext
from app.shared.c09_c10_common import Outcome
from app.shared.result import Failure, Ok, Result

from .models import Plan, PlanScope
from .store import DecisionRejectedError, PostgresPlanStore, lock_plan, scope_key
from .versions import save_ready_version

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Confirmation:
    plan: Plan
    correlation_id: UUID
    new_event_id: UUID | None


class _DeliveryRejectedError(Exception):
    """Roll back business writes before recording the failed C04 call."""

    def __init__(self, failure: Failure, ctx: ServiceContext, plan: Plan):
        self.failure = failure
        self.ctx = ctx
        self.plan = plan
        super().__init__(failure.message)


def _audit_confirmation(
    conn: psycopg.Connection,
    decisions: DecisionRecords,
    context: AuthContext,
    plan_id: UUID | None,
    outcome: Outcome,
    now: datetime,
) -> None:
    actor = ServiceContext(
        principal_id=str(context.principal_id),
        principal_type="User" if context.principal_type == "user" else "Service",
        roles=[*context.roles, *context.platform_roles],
        correlation_id=context.correlation_id,
    )
    result = decisions.record_outcome(
        conn,
        actor,
        AuditOutcome(
            operation="C05.confirmPlan",
            outcome=outcome,
            target_ids=[str(plan_id)] if plan_id is not None else [],
            occurred_at=now,
        ),
    )
    if isinstance(result, Failure):
        raise DecisionRejectedError(result)


def reject_confirmation(
    store: PostgresPlanStore,
    decisions: DecisionRecords,
    clock: Callable[[], datetime],
    context: AuthContext,
    plan_id: UUID | None,
    failure: Failure,
    delivery_rejection: _DeliveryRejectedError | None = None,
) -> Failure:
    """Persist the authenticated command's rejection after any business rollback."""
    try:
        with store.transaction() as conn:
            now = clock()
            outcome = Outcome(failure.kind)
            if delivery_rejection is not None:
                result = deliveries.audit_link(
                    conn,
                    decisions,
                    delivery_rejection.ctx,
                    delivery_rejection.plan.plan_id,
                    delivery_rejection.plan.request_ids,
                    outcome,
                    now,
                )
                if isinstance(result, Failure):
                    raise DecisionRejectedError(result)
            _audit_confirmation(conn, decisions, context, plan_id, outcome, now)
    except (DecisionRejectedError, psycopg.Error):
        return Failure("Unavailable", "C05.CONFIRMATION_AUDIT_UNAVAILABLE")
    return failure


def _commit_confirmation(
    conn: psycopg.Connection,
    context: AuthContext,
    scope: PlanScope,
    plan_id: UUID,
    expected_version: int,
    key: str,
    decisions: DecisionRecords,
    clock: Callable[[], datetime],
) -> Result[_Confirmation]:
    scoped = scope_key(scope.scope, scope.run_id)
    caller = (scoped, context.principal_id, context.acting_provider_id, key)
    # READ COMMITTED: statements after each lock see the previous caller's commit.
    # The key covers retries across plans. The plan lock covers competing keys.
    lock_key = json.dumps(
        [
            "C05.confirmPlan",
            scoped,
            str(context.principal_id),
            str(context.acting_provider_id),
            key,
        ]
    )
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
    previous = conn.execute(
        """select r.plan_id, r.expected_version, v.proposal, v.correlation_id
             from c05_planning.confirmation_receipts r
             join c05_planning.plan_versions v using (scope_key, plan_id, version)
            where r.scope_key = %s and r.principal_id = %s and r.provider_id = %s
              and r.idempotency_key = %s""",
        caller,
    ).fetchone()
    if previous:
        if previous[:2] != (plan_id, expected_version):
            return Failure("Conflict", "C05.IDEMPOTENCY_MISMATCH")
        saved = Plan.model_validate(previous[2], strict=False)
        return Ok(_Confirmation(saved, previous[3], None))
    lock_plan(conn, scope, plan_id)
    row = conn.execute(
        """select proposal, correlation_id, planned_departure_at
             from c05_planning.plan_versions
            where scope_key = %s and plan_id = %s order by version desc limit 1""",
        (scoped, plan_id),
    ).fetchone()
    if row is None:
        return Failure("NotFound", "C05.PLAN_NOT_FOUND")
    proposal = Plan.model_validate(row[0], strict=False)
    if (
        proposal.version != expected_version
        or proposal.status != "awaiting_confirmation"
    ):
        return Failure("Conflict", "C05.PROPOSAL_CHANGED")
    correlation_id, departure = row[1:]
    if correlation_id.version != 4:
        return Failure("Invalid", "C05.INVALID_DELIVERY_TRACE")
    now = clock()
    c05 = ServiceContext(
        principal_id="service:c05",
        principal_type="Service",
        roles=["Service:C05"],
        correlation_id=correlation_id,
    )
    try:
        result = deliveries.mark_requests_planned(
            conn,
            c05,
            scope,
            proposal.request_ids,
            plan_id,
            {
                did: proposal.input_versions[f"delivery:{did}"]
                for did in proposal.request_ids
            },
            f"c05:confirm:{plan_id}:v{expected_version}",
            decisions=decisions,
            now=now,
        )
    except psycopg.Error as exc:
        raise _DeliveryRejectedError(
            Failure("Unavailable", "C04.DELIVERY_STORE_UNAVAILABLE"), c05, proposal
        ) from exc
    if isinstance(result, Failure):
        raise _DeliveryRejectedError(result, c05, proposal)
    plan = proposal.model_copy(
        update={"version": expected_version + 1, "status": "confirmed"}
    )
    event_id = save_ready_version(conn, plan, departure, correlation_id, now)
    conn.execute(
        """insert into c05_planning.confirmation_receipts
                  (scope_key, principal_id, provider_id, idempotency_key, plan_id,
                   expected_version, version)
           values (%s,%s,%s,%s,%s,%s,%s)""",
        (*caller, plan_id, expected_version, plan.version),
    )
    _audit_confirmation(conn, decisions, context, plan_id, Outcome.OK, now)
    return Ok(_Confirmation(plan, correlation_id, event_id))


def confirm_plan(
    store: PostgresPlanStore,
    decisions: DecisionRecords,
    clock: Callable[[], datetime],
    context: AuthContext,
    run_scope: PlanScope,
    plan_id: UUID,
    expected_version: int,
    key: str,
) -> Result[Plan]:
    if isinstance(require_role(context, "operator"), Failure):
        failure = Failure("Forbidden", "C05.OPERATOR_REQUIRED")
    elif run_scope.scope != "simulation":
        failure = Failure("NotFound", "C05.PLAN_NOT_FOUND")
    elif not key.strip() or len(key) > 200 or expected_version < 1:
        failure = Failure("Invalid", "C05.INVALID_CONFIRMATION")
    else:
        failure = None
    if failure:
        return reject_confirmation(store, decisions, clock, context, plan_id, failure)
    try:
        with store.transaction() as conn:
            result = _commit_confirmation(
                conn,
                context,
                run_scope,
                plan_id,
                expected_version,
                key,
                decisions,
                clock,
            )
            if isinstance(result, Failure):
                raise DecisionRejectedError(result)
    except _DeliveryRejectedError as rejected:
        return reject_confirmation(
            store, decisions, clock, context, plan_id, rejected.failure, rejected
        )
    except DecisionRejectedError as rejected:
        return reject_confirmation(
            store, decisions, clock, context, plan_id, rejected.failure
        )
    except psycopg.Error:
        return reject_confirmation(
            store,
            decisions,
            clock,
            context,
            plan_id,
            Failure("Unavailable", "C05.CONFIRMATION_UNAVAILABLE"),
        )
    committed = result.value
    trace = {
        "component": "C05",
        "correlationId": str(committed.correlation_id),
        "scope": run_scope.scope,
        "runId": str(run_scope.run_id),
    }
    if committed.new_event_id is not None:
        trace.update(eventId=str(committed.new_event_id), eventType="PlanReady")
    log.info(
        "confirmPlan: plan %s v%d %s",
        plan_id,
        committed.plan.version,
        "confirmed, PlanReady queued"
        if committed.new_event_id
        else "confirmation replayed",
        extra=trace,
    )
    return Ok(committed.plan)
