"""In-process C04 → C05 planning handoff boundary for the walking skeleton (#72).

C04 invokes ``PlanningGateway.generate_plan`` after committing a new delivery.
``StubPlanningGateway`` is an explicit stand-in for isolated tests.
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid5

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.components.c04_delivery_management.contracts import Delivery, scope_key_for
from app.components.c10_decision_records.contracts import AuditOutcome
from app.components.c10_decision_records.service import DecisionRecords
from app.shared.c09_c10_common import AuthContext, Outcome, PrincipalType
from app.shared.contracts import ContractModel, RunScopeSelector, Version
from app.shared.result import Failure, Ok, Result

C05_SERVICE_PRINCIPAL_ID = UUID("00000000-0000-4000-8000-000000000005")
# Reserved provenance for imported samples. These identities have no C02 memberships.
_DEMO_PROVIDER_ID = UUID("07580262-aebe-57c8-84d4-49d45935ce3e")
_DEMO_IMPORTER_ID = UUID("cdff712f-a004-5818-ac58-c50d19694ec1")

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
    """Labelled stand-in for callers testing C04 without a C05 service."""

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
            "generatePlan (stub): plan %s v%d for delivery %s",
            summary.plan_id,
            summary.version,
            delivery.delivery_id,
            extra={
                "component": "C04",
                "correlationId": str(correlation_id),
                "scope": run_scope.scope,
                "runId": str(run_scope.run_id) if run_scope.run_id else None,
            },
        )
        return Ok(summary)


def _check_planning_scope(
    ctx: AuthContext, run_scope: RunScopeSelector
) -> Failure | None:
    if (
        ctx.principal_type is not PrincipalType.SERVICE
        or "Service:C05" not in ctx.roles
    ):
        return Failure("Forbidden", "C04.FORBIDDEN")
    if run_scope.scope != "simulation" or run_scope.run_id is None:
        return Failure("Invalid", "C04.INVALID_RUN_SCOPE")
    return None


def get_requests(
    conn: psycopg.Connection,
    ctx: AuthContext,
    run_scope: RunScopeSelector,
    request_ids: list[UUID],
    *,
    linked_to_plan: UUID | None = None,
) -> Result[list[dict]]:
    """Read all requested simulation deliveries on the caller's transaction.

    The optional plan selector returns only its current links, after checking
    that every requested ID exists in the run. Shared locks keep the progress
    and linkage read for a revision stable until that revision is committed.
    """
    rejected = _check_planning_scope(ctx, run_scope)
    if rejected:
        return rejected
    if not request_ids or len(set(request_ids)) != len(request_ids):
        return Failure("Invalid", "C04.INVALID_REQUEST")
    rows = conn.execute(
        """select delivery_id, delivery, plan_id from c04_delivery_management.deliveries
            where scope_key = %s and delivery_id = any(%s)
            order by delivery_id for share""",
        (scope_key_for(run_scope), request_ids),
    ).fetchall()
    if len(rows) != len(request_ids):
        return Failure("NotFound", "C04.DELIVERY_NOT_FOUND")
    included = {
        did: delivery
        for did, delivery, plan_id in rows
        if linked_to_plan is None or plan_id == linked_to_plan
    }
    return Ok([included[did] for did in request_ids if did in included])


def operator_can_access_requests(
    conn: psycopg.Connection,
    scope: RunScopeSelector,
    request_ids: list[UUID],
    provider_id: UUID | None,
    *,
    for_confirmation: bool = False,
) -> bool:
    """Check every delivery owner. Only a wholly imported run sample is shared."""
    if (
        provider_id is None
        or not request_ids
        or len(set(request_ids)) != len(request_ids)
    ):
        return False
    # Confirmation subsequently links these same rows. Lock them in C04's
    # update order now so competing confirmations never upgrade shared locks.
    lock = sql.SQL("for update") if for_confirmation else sql.SQL("for share")
    rows = conn.execute(
        sql.SQL(
            """select provider_id, created_by, initial_snapshot_hash is not null
                 from c04_delivery_management.deliveries
                where scope_key = %s and delivery_id = any(%s)
                order by delivery_id {}"""
        ).format(lock),
        (scope_key_for(scope), request_ids),
    ).fetchall()
    if len(rows) != len(request_ids):
        return False
    if all(owner == provider_id and not imported for owner, _, imported in rows):
        return True
    return scope.scope == "simulation" and all(
        imported and owner == _DEMO_PROVIDER_ID and creator == _DEMO_IMPORTER_ID
        for owner, creator, imported in rows
    )


def seed_demo_delivery(conn: psycopg.Connection, run_id: UUID, delivery: dict) -> None:
    """Import a saved snapshot once, preserving later updates to that delivery."""
    sample = Delivery.model_validate(delivery)
    key = f"simulation:{run_id}"
    if delivery["status"] != "pending" or delivery["progress"]["status"] != "pending":
        raise ValueError("The Dublin input must be a saved pending delivery.")
    fingerprint = hashlib.sha256(
        json.dumps(delivery, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    conn.execute(
        """insert into c04_delivery_management.deliveries
               (scope_key, scope, run_id, delivery_id, provider_id, created_by, version,
                status, planned_at, delivery, created_at, updated_at,
                initial_snapshot_hash)
           values (%s,'simulation',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (scope_key, delivery_id) do nothing""",
        (
            key,
            run_id,
            sample.delivery_id,
            _DEMO_PROVIDER_ID,
            _DEMO_IMPORTER_ID,
            sample.version,
            sample.status,
            sample.planned_at,
            Jsonb(delivery),
            sample.created_at,
            sample.updated_at,
            fingerprint,
        ),
    )
    (previous,) = conn.execute(
        """select initial_snapshot_hash from c04_delivery_management.deliveries
            where scope_key = %s and delivery_id = %s""",
        (key, sample.delivery_id),
    ).fetchone()
    if previous != fingerprint:
        raise ValueError(
            "This run already contains a different saved delivery snapshot."
        )


def audit_link(
    conn: psycopg.Connection,
    decisions: DecisionRecords,
    ctx: AuthContext,
    plan_id: UUID,
    request_ids: list[UUID],
    outcome: Outcome,
    now: datetime,
):
    return decisions.record_outcome(
        conn,
        ctx,
        AuditOutcome(
            operation="C04.markRequestsPlanned",
            outcome=outcome,
            target_ids=[str(plan_id), *(str(i) for i in request_ids)],
            occurred_at=now,
        ),
    )
