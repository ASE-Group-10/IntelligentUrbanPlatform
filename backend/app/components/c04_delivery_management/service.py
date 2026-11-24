"""C04 Delivery Management domain service, HTTP commands, and internal interfaces.

Implements the contracts in ``contracts/components/c04-delivery-management/``:
- HTTP operations: ``submit_delivery``, ``list_deliveries``, ``get_delivery``,
  ``update_delivery`` (``openapi.yaml``).
- Internal interfaces: ``get_requests``, ``mark_requests_planned``,
  ``update_progress`` (``internal.md``).
- Outbox events: ``DeliverySubmitted``, ``DeliveryUpdated``,
  ``DeliveryProgressChanged`` (``events.yaml``).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.components.c04_delivery_management.contracts import (
    Delivery,
    DeliveryEvent,
    DeliveryEventPayload,
    DeliveryEventType,
    DeliveryLinkReceipt,
    DeliveryLocation,
    DeliveryPage,
    DeliveryProgress,
    DeliveryStatus,
    ProgressStatus,
    SubmitDeliveryRequest,
    UpdateDeliveryRequest,
    scope_key_for,
)
from app.components.c04_delivery_management.planning import (
    PlanningGateway,
    PlanSummary,
    StubPlanningGateway,
)
from app.components.c04_delivery_management.store import (
    advisory_lock,
    audit_outcomes,
    command_receipts,
    deliveries,
    outbox,
)
from app.infrastructure.errors import APIError
from app.shared.auth import AuthContext, require_role
from app.shared.contracts import Affected, RunScopeSelector
from app.shared.result import Failure, Ok, Result

log = logging.getLogger("ulop.c04")

LocationOutcome = Literal["matched", "unmatched", "unconfirmed"]

_ALLOWED_PROGRESS_TRANSITIONS: dict[ProgressStatus, set[ProgressStatus]] = {
    "pending": {"picked_up", "in_transit", "delivered", "failed"},
    "picked_up": {"in_transit", "delivered", "failed"},
    "in_transit": {"delivered", "failed"},
    "delivered": set(),
    "failed": set(),
}


class DeliveryLocationValidator(Protocol):
    def validate_locations(
        self, pickup: DeliveryLocation, destination: DeliveryLocation
    ) -> None: ...


class MockDeliveryLocationValidator:
    """Development/walking-skeleton substitute for the mapping adapter.

    Accepts valid coordinates with distinct pickup and destination unless a
    test selects ``unmatched`` or ``unconfirmed``.
    """

    def __init__(self, outcome: LocationOutcome = "matched") -> None:
        self.outcome = outcome

    def validate_locations(
        self, pickup: DeliveryLocation, destination: DeliveryLocation
    ) -> None:
        if self.outcome == "unconfirmed":
            raise APIError(
                503,
                "C04.LOCATION_UNCONFIRMED",
                "Location validation service is unavailable.",
            )
        if self.outcome == "unmatched":
            raise APIError(
                422,
                "C04.LOCATION_UNMATCHED",
                "Pickup or destination location could not be validated.",
            )
        if (
            abs(pickup.latitude - destination.latitude) < 1e-6
            and abs(pickup.longitude - destination.longitude) < 1e-6
        ):
            raise APIError(
                422,
                "C04.IDENTICAL_LOCATIONS",
                "Pickup and destination locations must be different.",
            )


class _RollbackError(Exception):
    """Signal a transaction rollback while carrying a domain ``Failure``."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass(frozen=True)
class SubmitOutcome:
    status_code: int
    delivery: Delivery
    replayed: bool
    plan: PlanSummary | None = None


@dataclass(frozen=True)
class UpdateOutcome:
    status_code: int
    delivery: Delivery
    replayed: bool


def _digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _trace_extra(
    correlation_id: UUID, run_scope: RunScopeSelector
) -> dict[str, str | None]:
    return {
        "component": "C04",
        "correlationId": str(correlation_id),
        "scope": run_scope.scope,
        "runId": str(run_scope.run_id) if run_scope.run_id else None,
    }


def _can_mutate_deliveries(context: AuthContext) -> bool:
    return isinstance(require_role(context, "operator"), Ok) or isinstance(
        require_role(context, "manager"), Ok
    )


def _is_c05_service(context: AuthContext) -> bool:
    return context.principal_type == "service" and context.service_id == "C05"


def _is_progress_updater(context: AuthContext, provider_id: UUID) -> bool:
    if context.principal_type == "service":
        return True
    if context.acting_provider_id != provider_id:
        return False
    return (
        isinstance(require_role(context, "operator", provider_id), Ok)
        or isinstance(require_role(context, "manager", provider_id), Ok)
        or isinstance(require_role(context, "driver", provider_id), Ok)
    )


class DeliveryService:
    """C04 authoritative delivery store, HTTP commands, and internal interfaces."""

    def __init__(
        self,
        engine: Engine,
        *,
        planning_gateway: PlanningGateway | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.planning_gateway: PlanningGateway = (
            planning_gateway or StubPlanningGateway()
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    def _record_audit(
        self,
        connection: Connection,
        *,
        scope_key: str,
        provider_id: UUID | None,
        principal_id: str,
        operation: str,
        outcome: str,
        target_ids: list[str],
        correlation_id: UUID,
        occurred_at: datetime,
        details: dict | None = None,
    ) -> None:
        connection.execute(
            insert(audit_outcomes).values(
                id=uuid4(),
                scope_key=scope_key,
                provider_id=provider_id,
                principal_id=principal_id,
                operation=operation,
                outcome=outcome,
                target_ids=target_ids,
                correlation_id=correlation_id,
                details=details,
                occurred_at=occurred_at,
            )
        )

    def _record_rejected_audit(
        self,
        *,
        run_scope: RunScopeSelector,
        context: AuthContext,
        operation: str,
        outcome: str,
        target_ids: list[str],
        correlation_id: UUID,
        details: dict | None = None,
    ) -> None:
        """Record a rejected operation in its own short transaction (internal.md)."""
        try:
            with self.engine.begin() as connection:
                self._record_audit(
                    connection,
                    scope_key=scope_key_for(run_scope),
                    provider_id=context.acting_provider_id,
                    principal_id=str(context.principal_id),
                    operation=operation,
                    outcome=outcome,
                    target_ids=target_ids,
                    correlation_id=correlation_id,
                    occurred_at=self._now(),
                    details=details,
                )
        except SQLAlchemyError:
            log.warning(
                "failed to persist rejected audit outcome for %s",
                operation,
                extra=_trace_extra(correlation_id, run_scope),
            )

    def _enqueue_event(
        self,
        connection: Connection,
        *,
        event_type: DeliveryEventType,
        delivery: Delivery,
        run_scope: RunScopeSelector,
        correlation_id: UUID,
        observed_at: datetime,
        emitted_at: datetime,
        plan_id: UUID | None = None,
    ) -> DeliveryEvent:
        envelope = DeliveryEvent(
            event_id=uuid4(),
            event_type=event_type,
            schema_version=1,
            producer="C04",
            observed_at=observed_at,
            emitted_at=emitted_at,
            correlation_id=correlation_id,
            causation_id=None,
            affected=Affected(
                plan_ids=[plan_id] if plan_id is not None else [],
                resource_ids=[],
                segment_keys=[],
            ),
            entity_versions={str(delivery.delivery_id): delivery.version},
            scope=run_scope.scope,
            run_id=run_scope.run_id,
            payload=DeliveryEventPayload(
                delivery_id=delivery.delivery_id,
                delivery_version=delivery.version,
                status=delivery.status,
                delivery=delivery,
            ),
        )
        wire = envelope.model_dump(mode="json", by_alias=True)
        wire["payload"]["delivery"] = delivery.to_wire()
        connection.execute(
            insert(outbox).values(
                event_id=envelope.event_id,
                scope_key=scope_key_for(run_scope),
                delivery_id=delivery.delivery_id,
                delivery_version=delivery.version,
                event_type=event_type,
                correlation_id=correlation_id,
                envelope=wire,
                created_at=emitted_at,
                published_at=None,
            )
        )
        return envelope

    # ------------------------------------------------------------------
    # HTTP operations (contracts/components/c04-delivery-management/openapi.yaml)
    # ------------------------------------------------------------------

    def submit_delivery(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        command: SubmitDeliveryRequest,
        correlation_id: UUID,
        location_validator: DeliveryLocationValidator | None = None,
    ) -> SubmitOutcome:
        operation = "C04.submitDeliveryRequest"
        if not _can_mutate_deliveries(context) or context.acting_provider_id is None:
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Forbidden",
                target_ids=[],
                correlation_id=correlation_id,
            )
            raise APIError(403, "C04.FORBIDDEN", "Access denied.")

        validator = location_validator or MockDeliveryLocationValidator()
        try:
            validator.validate_locations(command.pickup, command.destination)
        except APIError as error:
            outcome = "Unavailable" if error.status == 503 else "Invalid"
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome=outcome,
                target_ids=[],
                correlation_id=correlation_id,
                details={"code": error.code},
            )
            raise

        skey = scope_key_for(run_scope)
        principal = str(context.principal_id)
        request_payload = command.model_dump(
            mode="json", by_alias=True, exclude={"idempotency_key"}
        )
        request_hash = _digest(
            {"providerId": str(context.acting_provider_id), "body": request_payload}
        )

        replayed = False
        with self.engine.begin() as connection:
            advisory_lock(
                connection,
                json.dumps([operation, skey, principal, command.idempotency_key]),
            )
            previous = (
                connection.execute(
                    select(command_receipts).where(
                        command_receipts.c.scope_key == skey,
                        command_receipts.c.principal_id == principal,
                        command_receipts.c.operation == operation,
                        command_receipts.c.idempotency_key == command.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    conflict = True
                    saved_delivery = None
                else:
                    conflict = False
                    replayed = True
                    saved_delivery = Delivery.model_validate(previous["response_body"])
            else:
                conflict = False
                timestamp = self._now()
                delivery = Delivery(
                    delivery_id=uuid4(),
                    version=1,
                    status="pending",
                    pickup=command.pickup,
                    destination=command.destination,
                    cargo=command.cargo,
                    time_window=command.time_window,
                    constraints=command.constraints,
                    planned_at=None,
                    progress=DeliveryProgress(
                        status="pending",
                        updated_at=timestamp,
                        note=None,
                    ),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                wire = delivery.to_wire()
                connection.execute(
                    insert(deliveries).values(
                        scope_key=skey,
                        scope=run_scope.scope,
                        run_id=run_scope.run_id,
                        delivery_id=delivery.delivery_id,
                        provider_id=context.acting_provider_id,
                        created_by=context.principal_id,
                        version=delivery.version,
                        status=delivery.status,
                        plan_id=None,
                        planned_at=None,
                        delivery=wire,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
                self._enqueue_event(
                    connection,
                    event_type="DeliverySubmitted",
                    delivery=delivery,
                    run_scope=run_scope,
                    correlation_id=correlation_id,
                    observed_at=timestamp,
                    emitted_at=timestamp,
                )
                self._record_audit(
                    connection,
                    scope_key=skey,
                    provider_id=context.acting_provider_id,
                    principal_id=principal,
                    operation=operation,
                    outcome="Ok",
                    target_ids=[str(delivery.delivery_id)],
                    correlation_id=correlation_id,
                    occurred_at=timestamp,
                )
                connection.execute(
                    insert(command_receipts).values(
                        scope_key=skey,
                        principal_id=principal,
                        operation=operation,
                        idempotency_key=command.idempotency_key,
                        request_hash=request_hash,
                        response_status=201,
                        response_body=wire,
                        created_at=timestamp,
                    )
                )
                saved_delivery = delivery

        if conflict:
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Conflict",
                target_ids=[],
                correlation_id=correlation_id,
                details={"code": "C04.IDEMPOTENCY_CONFLICT"},
            )
            raise APIError(
                409,
                "C04.IDEMPOTENCY_CONFLICT",
                "This idempotencyKey was already used with a different request.",
            )

        assert saved_delivery is not None
        log.info(
            "submitDelivery: delivery %s v%d %s",
            saved_delivery.delivery_id,
            saved_delivery.version,
            "replayed" if replayed else "persisted",
            extra=_trace_extra(correlation_id, run_scope),
        )

        # In-process C04 → C05 planning handoff after durable C04 commit.
        plan_summary: PlanSummary | None = None
        plan_result = self.planning_gateway.generate_plan(
            saved_delivery,
            run_scope,
            correlation_id,
            f"c04:{saved_delivery.delivery_id}:v{saved_delivery.version}",
        )
        if isinstance(plan_result, Ok):
            plan_summary = plan_result.value
        else:
            log.warning(
                "submitDelivery: C05 generatePlan returned %s (%s) for delivery %s",
                plan_result.kind,
                plan_result.message,
                saved_delivery.delivery_id,
                extra=_trace_extra(correlation_id, run_scope),
            )

        return SubmitOutcome(
            status_code=200 if replayed else 201,
            delivery=saved_delivery,
            replayed=replayed,
            plan=plan_summary,
        )

    def list_deliveries(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        *,
        status: DeliveryStatus | None = None,
        limit: int = 20,
        cursor: UUID | None = None,
    ) -> DeliveryPage:
        if context.acting_provider_id is None:
            raise APIError(403, "C04.FORBIDDEN", "Access denied.")
        skey = scope_key_for(run_scope)
        query = select(deliveries.c.delivery_id, deliveries.c.delivery).where(
            deliveries.c.scope_key == skey,
            deliveries.c.provider_id == context.acting_provider_id,
        )
        if status is not None:
            query = query.where(deliveries.c.status == status)
        if cursor is not None:
            query = query.where(deliveries.c.delivery_id > cursor)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    query.order_by(deliveries.c.delivery_id).limit(limit + 1)
                )
                .mappings()
                .all()
            )
        items = [Delivery.model_validate(row["delivery"]) for row in rows[:limit]]
        next_cursor = str(rows[limit - 1]["delivery_id"]) if len(rows) > limit else None
        return DeliveryPage(items=items, next_cursor=next_cursor)

    def get_delivery(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        delivery_id: UUID,
    ) -> Delivery:
        if context.acting_provider_id is None:
            raise APIError(403, "C04.FORBIDDEN", "Access denied.")
        skey = scope_key_for(run_scope)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(deliveries.c.delivery).where(
                        deliveries.c.scope_key == skey,
                        deliveries.c.delivery_id == delivery_id,
                        deliveries.c.provider_id == context.acting_provider_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise APIError(404, "C04.DELIVERY_NOT_FOUND", "Delivery request not found.")
        return Delivery.model_validate(row["delivery"])

    def update_delivery(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        delivery_id: UUID,
        command: UpdateDeliveryRequest,
        correlation_id: UUID,
        location_validator: DeliveryLocationValidator | None = None,
    ) -> UpdateOutcome:
        operation = "C04.updateDeliveryRequest"
        receipt_operation = f"{operation}:{delivery_id}"
        if not _can_mutate_deliveries(context) or context.acting_provider_id is None:
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Forbidden",
                target_ids=[str(delivery_id)],
                correlation_id=correlation_id,
            )
            raise APIError(403, "C04.FORBIDDEN", "Access denied.")

        validator = location_validator or MockDeliveryLocationValidator()
        try:
            validator.validate_locations(command.pickup, command.destination)
        except APIError as error:
            outcome = "Unavailable" if error.status == 503 else "Invalid"
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome=outcome,
                target_ids=[str(delivery_id)],
                correlation_id=correlation_id,
                details={"code": error.code},
            )
            raise

        skey = scope_key_for(run_scope)
        principal = str(context.principal_id)
        request_payload = command.model_dump(
            mode="json", by_alias=True, exclude={"idempotency_key"}
        )
        request_hash = _digest(
            {
                "providerId": str(context.acting_provider_id),
                "deliveryId": str(delivery_id),
                "body": request_payload,
            }
        )

        error_to_raise: tuple[int, str, str, str] | None = None
        replayed = False
        updated_delivery: Delivery | None = None

        with self.engine.begin() as connection:
            advisory_lock(
                connection,
                json.dumps(
                    [receipt_operation, skey, principal, command.idempotency_key]
                ),
            )
            previous = (
                connection.execute(
                    select(command_receipts).where(
                        command_receipts.c.scope_key == skey,
                        command_receipts.c.principal_id == principal,
                        command_receipts.c.operation == receipt_operation,
                        command_receipts.c.idempotency_key == command.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    error_to_raise = (
                        409,
                        "C04.IDEMPOTENCY_CONFLICT",
                        "This idempotencyKey was already used with different input.",
                        "Conflict",
                    )
                else:
                    replayed = True
                    updated_delivery = Delivery.model_validate(
                        previous["response_body"]
                    )
            else:
                row = (
                    connection.execute(
                        select(deliveries).where(
                            deliveries.c.scope_key == skey,
                            deliveries.c.delivery_id == delivery_id,
                            deliveries.c.provider_id == context.acting_provider_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    error_to_raise = (
                        404,
                        "C04.DELIVERY_NOT_FOUND",
                        "Delivery request not found.",
                        "NotFound",
                    )
                elif row["status"] != "pending":
                    error_to_raise = (
                        409,
                        "C04.DELIVERY_NOT_PENDING",
                        "Only pending delivery requests can be updated.",
                        "Conflict",
                    )
                elif row["version"] != command.expected_version:
                    error_to_raise = (
                        409,
                        "C04.VERSION_CONFLICT",
                        "Expected version does not match the current delivery.",
                        "Conflict",
                    )
                else:
                    current = Delivery.model_validate(row["delivery"])
                    timestamp = self._now()
                    next_delivery = Delivery(
                        delivery_id=current.delivery_id,
                        version=current.version + 1,
                        status=current.status,
                        pickup=command.pickup,
                        destination=command.destination,
                        cargo=command.cargo,
                        time_window=command.time_window,
                        constraints=command.constraints,
                        planned_at=current.planned_at,
                        progress=current.progress,
                        created_at=current.created_at,
                        updated_at=timestamp,
                    )
                    wire = next_delivery.to_wire()
                    result = connection.execute(
                        update(deliveries)
                        .where(
                            deliveries.c.scope_key == skey,
                            deliveries.c.delivery_id == delivery_id,
                            deliveries.c.provider_id == context.acting_provider_id,
                            deliveries.c.version == command.expected_version,
                            deliveries.c.status == "pending",
                        )
                        .values(
                            version=next_delivery.version,
                            delivery=wire,
                            updated_at=timestamp,
                        )
                    )
                    if result.rowcount != 1:
                        error_to_raise = (
                            409,
                            "C04.VERSION_CONFLICT",
                            "Another update won. Reload and retry.",
                            "Conflict",
                        )
                    else:
                        self._enqueue_event(
                            connection,
                            event_type="DeliveryUpdated",
                            delivery=next_delivery,
                            run_scope=run_scope,
                            correlation_id=correlation_id,
                            observed_at=timestamp,
                            emitted_at=timestamp,
                        )
                        self._record_audit(
                            connection,
                            scope_key=skey,
                            provider_id=context.acting_provider_id,
                            principal_id=principal,
                            operation=operation,
                            outcome="Ok",
                            target_ids=[str(delivery_id)],
                            correlation_id=correlation_id,
                            occurred_at=timestamp,
                        )
                        connection.execute(
                            insert(command_receipts).values(
                                scope_key=skey,
                                principal_id=principal,
                                operation=receipt_operation,
                                idempotency_key=command.idempotency_key,
                                request_hash=request_hash,
                                response_status=200,
                                response_body=wire,
                                created_at=timestamp,
                            )
                        )
                        updated_delivery = next_delivery

        if error_to_raise is not None:
            status_code, code, message, audit_outcome = error_to_raise
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome=audit_outcome,
                target_ids=[str(delivery_id)],
                correlation_id=correlation_id,
                details={"code": code},
            )
            raise APIError(status_code, code, message)

        assert updated_delivery is not None
        log.info(
            "updateDelivery: delivery %s v%d %s",
            updated_delivery.delivery_id,
            updated_delivery.version,
            "replayed" if replayed else "updated",
            extra=_trace_extra(correlation_id, run_scope),
        )
        return UpdateOutcome(
            status_code=200, delivery=updated_delivery, replayed=replayed
        )

    # ------------------------------------------------------------------
    # Internal interfaces (contracts/components/c04-delivery-management/internal.md)
    # ------------------------------------------------------------------

    def get_requests(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        request_ids: list[UUID],
    ) -> Result[list[Delivery]]:
        if not request_ids:
            return Failure("Invalid", "C04.INVALID_REQUEST")
        if context.principal_type != "service" and context.acting_provider_id is None:
            return Failure("Forbidden", "C04.FORBIDDEN")

        skey = scope_key_for(run_scope)
        unique_ids = list(dict.fromkeys(request_ids))
        query = select(deliveries.c.delivery_id, deliveries.c.delivery).where(
            deliveries.c.scope_key == skey,
            deliveries.c.delivery_id.in_(unique_ids),
        )
        if context.principal_type != "service":
            query = query.where(deliveries.c.provider_id == context.acting_provider_id)

        try:
            with self.engine.connect() as connection:
                rows = connection.execute(query).mappings().all()
        except SQLAlchemyError:
            return Failure("Unavailable", "C04.STORE_UNAVAILABLE")

        found = {
            row["delivery_id"]: Delivery.model_validate(row["delivery"]) for row in rows
        }
        if len(found) != len(unique_ids):
            return Failure("NotFound", "C04.DELIVERY_NOT_FOUND")
        return Ok([found[request_id] for request_id in request_ids])

    def mark_requests_planned(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        request_ids: list[UUID],
        plan_id: UUID,
        expected_versions: dict[UUID, int],
        key: str,
        *,
        connection: Connection | None = None,
    ) -> Result[DeliveryLinkReceipt]:
        operation = "C04.markRequestsPlanned"
        if not _is_c05_service(context):
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Forbidden",
                target_ids=[str(plan_id), *(str(rid) for rid in request_ids)],
                correlation_id=context.correlation_id,
            )
            return Failure("Forbidden", "C04.FORBIDDEN")

        if (
            not request_ids
            or len(set(request_ids)) != len(request_ids)
            or set(request_ids) != expected_versions.keys()
            or not key.strip()
            or len(key) > 200
            or any(v < 1 for v in expected_versions.values())
        ):
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Invalid",
                target_ids=[str(plan_id), *(str(rid) for rid in request_ids)],
                correlation_id=context.correlation_id,
            )
            return Failure("Invalid", "C04.INVALID_REQUEST")

        skey = scope_key_for(run_scope)
        principal = str(context.principal_id)
        request_hash = _digest(
            {
                "requestIds": [str(rid) for rid in request_ids],
                "planId": str(plan_id),
                "expectedVersions": {
                    str(rid): ver for rid, ver in expected_versions.items()
                },
            }
        )

        def _execute(conn: Connection) -> Result[DeliveryLinkReceipt]:
            advisory_lock(conn, json.dumps([operation, skey, principal, key]))
            previous = (
                conn.execute(
                    select(command_receipts).where(
                        command_receipts.c.scope_key == skey,
                        command_receipts.c.principal_id == principal,
                        command_receipts.c.operation == operation,
                        command_receipts.c.idempotency_key == key,
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    return Failure("Conflict", "C04.IDEMPOTENCY_CONFLICT")
                return Ok(DeliveryLinkReceipt.model_validate(previous["response_body"]))

            rows = (
                conn.execute(
                    select(deliveries)
                    .where(
                        deliveries.c.scope_key == skey,
                        deliveries.c.delivery_id.in_(request_ids),
                    )
                    .order_by(deliveries.c.delivery_id)
                )
                .mappings()
                .all()
            )
            by_id = {row["delivery_id"]: row for row in rows}
            if len(by_id) != len(request_ids):
                return Failure("NotFound", "C04.DELIVERY_NOT_FOUND")

            for rid in request_ids:
                row = by_id[rid]
                if row["status"] != "pending":
                    return Failure("Conflict", "C04.DELIVERY_NOT_PENDING")
                if row["version"] != expected_versions[rid]:
                    return Failure("Conflict", "C04.VERSION_CONFLICT")

            timestamp = self._now()
            new_versions: dict[UUID, int] = {}
            for rid in request_ids:
                current = Delivery.model_validate(by_id[rid]["delivery"])
                next_version = current.version + 1
                next_delivery = Delivery(
                    delivery_id=current.delivery_id,
                    version=next_version,
                    status="planned",
                    pickup=current.pickup,
                    destination=current.destination,
                    cargo=current.cargo,
                    time_window=current.time_window,
                    constraints=current.constraints,
                    planned_at=timestamp,
                    progress=current.progress,
                    created_at=current.created_at,
                    updated_at=timestamp,
                )
                result = conn.execute(
                    update(deliveries)
                    .where(
                        deliveries.c.scope_key == skey,
                        deliveries.c.delivery_id == rid,
                        deliveries.c.version == expected_versions[rid],
                        deliveries.c.status == "pending",
                    )
                    .values(
                        version=next_version,
                        status="planned",
                        plan_id=plan_id,
                        planned_at=timestamp,
                        delivery=next_delivery.to_wire(),
                        updated_at=timestamp,
                    )
                )
                if result.rowcount != 1:
                    raise _RollbackError(Failure("Conflict", "C04.VERSION_CONFLICT"))
                new_versions[rid] = next_version

            receipt = DeliveryLinkReceipt(
                request_ids=list(request_ids),
                plan_id=plan_id,
                versions=new_versions,
            )
            conn.execute(
                insert(command_receipts).values(
                    scope_key=skey,
                    principal_id=principal,
                    operation=operation,
                    idempotency_key=key,
                    request_hash=request_hash,
                    response_status=200,
                    response_body=receipt.model_dump(mode="json", by_alias=True),
                    created_at=timestamp,
                )
            )
            self._record_audit(
                conn,
                scope_key=skey,
                provider_id=None,
                principal_id=principal,
                operation=operation,
                outcome="Ok",
                target_ids=[str(plan_id), *(str(rid) for rid in request_ids)],
                correlation_id=context.correlation_id,
                occurred_at=timestamp,
            )
            return Ok(receipt)

        try:
            if connection is not None:
                with connection.begin_nested():
                    outcome = _execute(connection)
            else:
                with self.engine.begin() as owned_conn:
                    outcome = _execute(owned_conn)
        except _RollbackError as aborted:
            outcome = aborted.failure
        except SQLAlchemyError:
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Unavailable",
                target_ids=[str(plan_id), *(str(rid) for rid in request_ids)],
                correlation_id=context.correlation_id,
            )
            return Failure("Unavailable", "C04.STORE_UNAVAILABLE")

        if isinstance(outcome, Failure):
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome=outcome.kind,
                target_ids=[str(plan_id), *(str(rid) for rid in request_ids)],
                correlation_id=context.correlation_id,
                details={"code": outcome.message},
            )
        else:
            log.info(
                "markRequestsPlanned: linked %d delivery request(s) to plan %s",
                len(request_ids),
                plan_id,
                extra=_trace_extra(context.correlation_id, run_scope),
            )
        return outcome

    def update_progress(
        self,
        context: AuthContext,
        run_scope: RunScopeSelector,
        delivery_id: UUID,
        progress: DeliveryProgress,
        key: str,
    ) -> Result[Delivery]:
        operation = "C04.updateProgress"
        receipt_operation = f"{operation}:{delivery_id}"
        if not key.strip() or len(key) > 200:
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Invalid",
                target_ids=[str(delivery_id)],
                correlation_id=context.correlation_id,
            )
            return Failure("Invalid", "C04.INVALID_REQUEST")

        skey = scope_key_for(run_scope)
        principal = str(context.principal_id)
        request_hash = _digest(
            {
                "deliveryId": str(delivery_id),
                "progress": progress.model_dump(mode="json", by_alias=True),
            }
        )

        try:
            with self.engine.begin() as connection:
                advisory_lock(
                    connection,
                    json.dumps([receipt_operation, skey, principal, key]),
                )
                previous = (
                    connection.execute(
                        select(command_receipts).where(
                            command_receipts.c.scope_key == skey,
                            command_receipts.c.principal_id == principal,
                            command_receipts.c.operation == receipt_operation,
                            command_receipts.c.idempotency_key == key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous is not None:
                    if previous["request_hash"] != request_hash:
                        result: Result[Delivery] = Failure(
                            "Conflict", "C04.IDEMPOTENCY_CONFLICT"
                        )
                    else:
                        result = Ok(Delivery.model_validate(previous["response_body"]))
                else:
                    row = (
                        connection.execute(
                            select(deliveries).where(
                                deliveries.c.scope_key == skey,
                                deliveries.c.delivery_id == delivery_id,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        result = Failure("NotFound", "C04.DELIVERY_NOT_FOUND")
                    elif not _is_progress_updater(context, row["provider_id"]):
                        result = Failure("Forbidden", "C04.FORBIDDEN")
                    elif row["status"] == "cancelled":
                        result = Failure("Conflict", "C04.DELIVERY_CANCELLED")
                    else:
                        current = Delivery.model_validate(row["delivery"])
                        allowed = _ALLOWED_PROGRESS_TRANSITIONS[current.progress.status]
                        if progress.status not in allowed:
                            result = Failure(
                                "Invalid", "C04.INVALID_PROGRESS_TRANSITION"
                            )
                        else:
                            timestamp = self._now()
                            next_status: DeliveryStatus = (
                                "delivered"
                                if progress.status == "delivered"
                                else "in_progress"
                            )
                            next_delivery = Delivery(
                                delivery_id=current.delivery_id,
                                version=current.version + 1,
                                status=next_status,
                                pickup=current.pickup,
                                destination=current.destination,
                                cargo=current.cargo,
                                time_window=current.time_window,
                                constraints=current.constraints,
                                planned_at=current.planned_at,
                                progress=progress,
                                created_at=current.created_at,
                                updated_at=timestamp,
                            )
                            wire = next_delivery.to_wire()
                            connection.execute(
                                update(deliveries)
                                .where(
                                    deliveries.c.scope_key == skey,
                                    deliveries.c.delivery_id == delivery_id,
                                    deliveries.c.version == current.version,
                                )
                                .values(
                                    version=next_delivery.version,
                                    status=next_delivery.status,
                                    delivery=wire,
                                    updated_at=timestamp,
                                )
                            )
                            self._enqueue_event(
                                connection,
                                event_type="DeliveryProgressChanged",
                                delivery=next_delivery,
                                run_scope=run_scope,
                                correlation_id=context.correlation_id,
                                observed_at=progress.updated_at,
                                emitted_at=timestamp,
                                plan_id=row["plan_id"],
                            )
                            self._record_audit(
                                connection,
                                scope_key=skey,
                                provider_id=row["provider_id"],
                                principal_id=principal,
                                operation=operation,
                                outcome="Ok",
                                target_ids=[str(delivery_id)],
                                correlation_id=context.correlation_id,
                                occurred_at=timestamp,
                            )
                            connection.execute(
                                insert(command_receipts).values(
                                    scope_key=skey,
                                    principal_id=principal,
                                    operation=receipt_operation,
                                    idempotency_key=key,
                                    request_hash=request_hash,
                                    response_status=200,
                                    response_body=wire,
                                    created_at=timestamp,
                                )
                            )
                            result = Ok(next_delivery)
        except SQLAlchemyError:
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome="Unavailable",
                target_ids=[str(delivery_id)],
                correlation_id=context.correlation_id,
            )
            return Failure("Unavailable", "C04.STORE_UNAVAILABLE")

        if isinstance(result, Failure):
            self._record_rejected_audit(
                run_scope=run_scope,
                context=context,
                operation=operation,
                outcome=result.kind,
                target_ids=[str(delivery_id)],
                correlation_id=context.correlation_id,
                details={"code": result.message},
            )
        else:
            log.info(
                "updateProgress: delivery %s v%d progress=%s",
                delivery_id,
                result.value.version,
                progress.status,
                extra=_trace_extra(context.correlation_id, run_scope),
            )
        return result
