"""C04's simulation delivery link for the walking skeleton.

The C05 caller supplies its PostgreSQL transaction. The demo importer stands
in for the C04 submit handler at the boundary of an already saved delivery.
"""

import hashlib
import json
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.components.c10_decision_records.contracts import AuditOutcome
from app.components.c10_decision_records.service import DecisionRecords
from app.shared.c09_c10_common import AuthContext, Outcome, PrincipalType
from app.shared.contracts import ContractModel, RunScopeSelector, Version
from app.shared.result import Failure, Ok, Result


class DeliveryLinkReceipt(ContractModel):
    request_ids: list[UUID]
    plan_id: UUID
    versions: dict[UUID, Version]


def seed_demo_delivery(conn: psycopg.Connection, run_id: UUID, delivery: dict) -> None:
    """Import a saved snapshot once, preserving later updates to that delivery."""
    delivery_id = UUID(str(delivery["deliveryId"]))
    if delivery["status"] != "pending" or delivery["progress"]["status"] != "pending":
        raise ValueError("The Dublin input must be a saved pending delivery.")
    fingerprint = hashlib.sha256(
        json.dumps(delivery, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    conn.execute(
        """insert into c04_delivery_management.deliveries
                  (run_id, delivery, initial_snapshot_hash) values (%s, %s, %s)
           on conflict (run_id, delivery_id) do nothing""",
        (run_id, Jsonb(delivery), fingerprint),
    )
    (previous,) = conn.execute(
        """select initial_snapshot_hash from c04_delivery_management.deliveries
            where run_id = %s and delivery_id = %s""",
        (run_id, delivery_id),
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


def mark_requests_planned(
    conn: psycopg.Connection,
    ctx: AuthContext,
    run_scope: RunScopeSelector,
    request_ids: list[UUID],
    plan_id: UUID,
    expected_versions: dict[UUID, int],
    key: str,
    *,
    decisions: DecisionRecords,
    now: datetime,
) -> Result[DeliveryLinkReceipt]:
    """Link pending C04 deliveries and audit the mutation on the caller's connection."""
    if (
        ctx.principal_type is not PrincipalType.SERVICE
        or "Service:C05" not in ctx.roles
    ):
        return Failure("Forbidden", "C04.FORBIDDEN")
    if run_scope.scope != "simulation" or run_scope.run_id is None:
        return Failure("Invalid", "C04.INVALID_RUN_SCOPE")
    run_id = run_scope.run_id
    if (
        not request_ids
        or len(set(request_ids)) != len(request_ids)
        or set(request_ids) != expected_versions.keys()
        or not key.strip()
    ):
        return Failure("Invalid", "C04.INVALID_REQUEST")
    body = {
        "requestIds": list(map(str, request_ids)),
        "planId": str(plan_id),
        "expectedVersions": {str(k): v for k, v in expected_versions.items()},
    }
    lock_name = json.dumps(
        ["C04.markRequestsPlanned", str(run_id), ctx.principal_id, key]
    )
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_name,))
    previous = conn.execute(
        """select request_body = %s, receipt
             from c04_delivery_management.planning_receipts
            where run_id = %s and principal_id = %s and idempotency_key = %s""",
        (Jsonb(body), run_id, ctx.principal_id, key),
    ).fetchone()
    if previous:
        if not previous[0]:
            return Failure("Conflict", "C04.IDEMPOTENCY_MISMATCH")
        return Ok(DeliveryLinkReceipt.model_validate(previous[1]))
    # Acquire all delivery locks in a stable order and validate all versions
    # before updating any row, including when two plans share a delivery.
    rows = conn.execute(
        """select delivery_id, delivery, plan_id from c04_delivery_management.deliveries
            where run_id = %s and delivery_id = any(%s)
            order by delivery_id for update""",
        (run_id, request_ids),
    ).fetchall()
    if len(rows) != len(request_ids):
        return Failure("NotFound", "C04.DELIVERY_NOT_FOUND")
    for did, delivery, linked_plan in rows:
        if (
            linked_plan is not None
            or delivery["status"] != "pending"
            or delivery["progress"]["status"] != "pending"
            or delivery["version"] != expected_versions[did]
        ):
            return Failure("Conflict", "C04.DELIVERY_CHANGED")
    for did, delivery, _ in rows:
        delivery.update(
            status="planned",
            version=expected_versions[did] + 1,
            plannedAt=now.isoformat(),
            updatedAt=now.isoformat(),
        )
        conn.execute(
            """update c04_delivery_management.deliveries set delivery = %s, plan_id = %s
                where run_id = %s and delivery_id = %s""",
            (Jsonb(delivery), plan_id, run_id, did),
        )
    receipt = DeliveryLinkReceipt(
        request_ids=request_ids,
        plan_id=plan_id,
        versions={did: version + 1 for did, version in expected_versions.items()},
    )
    conn.execute(
        """insert into c04_delivery_management.planning_receipts
            (run_id, principal_id, idempotency_key, request_body, receipt)
            values (%s,%s,%s,%s,%s)""",
        (
            run_id,
            ctx.principal_id,
            key,
            Jsonb(body),
            Jsonb(receipt.model_dump(mode="json", by_alias=True)),
        ),
    )
    audited = audit_link(conn, decisions, ctx, plan_id, request_ids, Outcome.OK, now)
    if isinstance(audited, Failure):
        return audited
    return Ok(receipt)
