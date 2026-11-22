"""PostgreSQL transactions for the single-vehicle, live PlanReady slice."""

import json
from decimal import Decimal

from sqlalchemy import select, text

from app.components.resources.service import ResourceService
from app.infrastructure.database import resources
from app.infrastructure.errors import APIError


def conflict(message):
    return APIError(409, "CONFLICT", message)


def lock_key(conn, value):
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"),
        {"value": value},
    )


class AllocationStore:
    def __init__(self, engine, freshness_seconds):
        self.engine = engine
        self.resources = ResourceService(engine, freshness_seconds)

    def vehicle(self, conn, provider, resource):
        conn.execute(
            text("SELECT set_config('app.provider_id', :provider, true)"),
            {"provider": str(provider)},
        )
        row = (
            conn.execute(
                select(resources)
                .where(resources.c.provider_id == provider, resources.c.id == resource)
                .with_for_update(read=True)
            )
            .mappings()
            .first()
        )
        if (
            row
            and row["resource_type"] == "vehicle"
            and self.resources.present(row)["planning_eligible"]
        ):
            return row
        return None

    def consume(self, event):
        if event.scope != "live":
            return "ignored simulation"
        payload = event.payload
        with self.engine.begin() as conn:
            plan = (
                conn.execute(
                    text("""
                SELECT * FROM c08_allocation.demo_plans WHERE plan_id=:plan FOR SHARE
            """),
                    {"plan": payload.plan_id},
                )
                .mappings()
                .first()
            )
            if not plan:
                return "outside configured demo"
            if payload.plan_version > plan["plan_version"]:
                return "plan unavailable"
            seen = conn.execute(
                text("""
                INSERT INTO c08_allocation.processed_events VALUES (:event,:correlation)
                ON CONFLICT DO NOTHING RETURNING event_id
            """),
                {"event": event.event_id, "correlation": event.correlation_id},
            ).scalar()
            if not seen:
                return "duplicate"
            if payload.plan_version < plan["plan_version"]:
                return "superseded"
            if len(payload.requirements) != 1:
                return "unsupported requirements"
            need = payload.requirements[0]
            if (
                need.delivery_id != plan["delivery_id"]
                or need.capacity.unit != "kg"
                or Decimal(str(need.capacity.amount)) != plan["required_kg"]
            ):
                return "unsupported requirements"
            # Serialize capacity checks for plans competing for the same vehicle.
            lock_key(conn, f"c08:capacity:{plan['resource_id']}")
            vehicle = self.vehicle(conn, plan["provider_id"], plan["resource_id"])
            if (
                not vehicle
                or vehicle["details"]["vehicle_category"] != need.resource_kind
            ):
                return "ineligible vehicle"
            used = conn.execute(
                text("""
                SELECT coalesce(sum(capacity_kg),0) FROM c08_allocation.allocations
                WHERE resource_id=:resource AND status <> 'rejected'
            """),
                {"resource": vehicle["id"]},
            ).scalar_one()
            kg = Decimal(str(need.capacity.amount))
            if used + kg > Decimal(str(vehicle["details"]["max_weight_kg"])):
                return "insufficient capacity"
            created = conn.execute(
                text("""
                INSERT INTO c08_allocation.allocations
                    (event_id,correlation_id,plan_id,plan_version,delivery_id,
                     provider_id,resource_id,resource_name,resource_version,driver_id,capacity_kg)
                VALUES (:event,:correlation,:plan_id,:plan_version,:delivery_id,
                     :provider_id,:resource_id,:name,:version,:driver_id,:kg)
                ON CONFLICT (plan_id,delivery_id) DO NOTHING RETURNING assignment_ref
            """),
                {
                    **plan,
                    "event": event.event_id,
                    "correlation": event.correlation_id,
                    "name": vehicle["name"],
                    "version": vehicle["version"],
                    "kg": kg,
                },
            ).scalar()
            return "allocated" if created else "already committed"

    def assignments(self, ctx, operator):
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    text("""
                SELECT plan_id, plan_version, delivery_id, assignment_ref,
                       resource_name, resource_id, offer_id, driver_id,
                       status, correlation_id
                FROM c08_allocation.allocations WHERE provider_id=:provider
                  AND (:operator OR (driver_id=:actor AND offer_id IS NOT NULL))
                ORDER BY plan_id, delivery_id
            """),
                    {
                        "provider": ctx.acting_provider_id,
                        "actor": ctx.principal_id,
                        "operator": operator,
                    },
                )
                .mappings()
                .all()
            )

    def command(self, ctx, key, operation, command, apply):
        receipt = {
            "provider": ctx.acting_provider_id,
            "actor": ctx.principal_id,
            "operation": operation,
            "key": key,
        }
        with self.engine.begin() as conn:
            lock_key(
                conn,
                f"c08:command:{ctx.acting_provider_id}:{ctx.principal_id}:{operation}:{key}",
            )
            previous = (
                conn.execute(
                    text("""
                SELECT command, outcome FROM c08_allocation.command_receipts
                WHERE (provider_id, principal_id, operation, key)
                    = (:provider, :actor, :operation, :key)
            """),
                    receipt,
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["command"] != command:
                    raise conflict(
                        "Idempotency key already used for a different command."
                    )
                return previous["outcome"]
            outcome = apply(conn)
            conn.execute(
                text("""
                INSERT INTO c08_allocation.command_receipts VALUES
                (:provider, :actor, :operation, :key,
                 CAST(:command AS jsonb), CAST(:outcome AS jsonb))
            """),
                {
                    **receipt,
                    "command": json.dumps(command),
                    "outcome": json.dumps(outcome),
                },
            )
            return outcome

    def check_current(self, conn, row):
        version = conn.execute(
            text("""
            SELECT plan_version FROM c08_allocation.demo_plans
            WHERE plan_id=:plan FOR SHARE
        """),
            {"plan": row["plan_id"]},
        ).scalar()
        vehicle = self.vehicle(conn, row["provider_id"], row["resource_id"])
        if (
            version != row["plan_version"]
            or not vehicle
            or vehicle["version"] != row["resource_version"]
        ):
            raise conflict("The saved plan or vehicle has changed.")

    def offer(self, ctx, body, key):
        def apply(conn):
            row = (
                conn.execute(
                    text("""
                SELECT * FROM c08_allocation.allocations
                WHERE plan_id=:plan AND delivery_id=:delivery
                AND provider_id=:provider FOR UPDATE
            """),
                    {
                        "plan": body.plan_id,
                        "delivery": body.delivery_id,
                        "provider": ctx.acting_provider_id,
                    },
                )
                .mappings()
                .first()
            )
            if not row or row["plan_version"] != body.expected_version:
                raise conflict(
                    "No current allocation for this delivery and plan version."
                )
            if row["status"] not in ("allocated", "offered"):
                raise conflict("This assignment already has a final response.")
            self.check_current(conn, row)
            if row["status"] == "allocated":
                conn.execute(
                    text("""
                    UPDATE c08_allocation.allocations
                    SET status='offered', offer_id=gen_random_uuid()
                    WHERE assignment_ref=:ref
                """),
                    {"ref": row["assignment_ref"]},
                )
            return outcome(row, "awaiting_acceptance", "awaiting_uc04_acceptance")

        return self.command(
            ctx, key, "assign", body.model_dump(mode="json", by_alias=True), apply
        )

    def respond(self, ctx, offer, response, key):
        def apply(conn):
            row = (
                conn.execute(
                    text("""
                SELECT * FROM c08_allocation.allocations
                WHERE offer_id=:offer AND provider_id=:provider
                  AND driver_id=:actor FOR UPDATE
            """),
                    {
                        "offer": offer,
                        "provider": ctx.acting_provider_id,
                        "actor": ctx.principal_id,
                    },
                )
                .mappings()
                .first()
            )
            if not row:
                raise APIError(403, "FORBIDDEN", "Access denied.")
            if row["status"] != "offered":
                raise conflict("The offer already has a final response.")
            accepted = response == "accept"
            if accepted:
                self.check_current(conn, row)
            status = "accepted" if accepted else "rejected"
            conn.execute(
                text("""
                UPDATE c08_allocation.allocations SET status=:status
                WHERE offer_id=:offer
            """),
                {"offer": offer, "status": status},
            )
            return outcome(row, "assigned" if accepted else "unassigned", status)

        return self.command(
            ctx, key, "respond", {"offerId": str(offer), "response": response}, apply
        )


def outcome(row, code, status):
    return {
        "code": code,
        "acceptanceStatus": status,
        "correlationId": str(row["correlation_id"]),
        "assignmentRef": str(row["assignment_ref"]),
        "allocationId": str(row["allocation_id"]),
        "resourceId": str(row["resource_id"]),
        "resultingPlanVersion": row["plan_version"],
        "resultingResourceVersions": {str(row["resource_id"]): row["resource_version"]},
    }
