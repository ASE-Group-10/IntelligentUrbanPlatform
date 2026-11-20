import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.components.resources.schemas import ResourceBase, resource_adapter
from app.infrastructure.database import audit, outbox, receipts, resources
from app.infrastructure.errors import APIError


def now():
    return datetime.now(UTC)


class ResourceService:
    def __init__(self, engine, freshness_seconds=86400):
        self.engine = engine
        self.freshness_seconds = freshness_seconds

    def _scope(self, connection, context):
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('app.provider_id', :provider, true)"),
                {"provider": str(context.provider_id)},
            )

    def _find(self, connection, context, resource_id):
        row = (
            connection.execute(
                select(resources).where(
                    resources.c.id == resource_id,
                    resources.c.provider_id == context.provider_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise APIError(404, "NOT_FOUND", "Resource not found.")
        return dict(row)

    def present(self, resource):
        reasons = []
        if resource["profile_status"] != "active":
            reasons.append("profile_inactive")
        if resource["agent_status"] != "active":
            reasons.append("agent_not_active")
        if resource["observed_availability"] != "available":
            reasons.append("availability_not_available")
        observed = resource["availability_observed_at"]
        if observed is not None:
            observed = (
                observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed
            )
        if (
            observed is None
            or (now() - observed).total_seconds() > self.freshness_seconds
        ):
            reasons.append("availability_stale_or_unknown")
        return jsonable_encoder(
            {
                **resource,
                "planning_eligible": not reasons,
                "eligibility_reasons": reasons,
            }
        )

    def get(self, context, resource_id):
        with self.engine.begin() as connection:
            self._scope(connection, context)
            return self.present(self._find(connection, context, resource_id))

    def list(
        self, context, resource_type=None, profile_status=None, limit=20, cursor=None
    ):
        query = select(resources).where(resources.c.provider_id == context.provider_id)
        if resource_type:
            query = query.where(resources.c.resource_type == resource_type)
        if profile_status:
            query = query.where(resources.c.profile_status == profile_status)
        if cursor:
            query = query.where(resources.c.id > cursor)
        with self.engine.begin() as connection:
            self._scope(connection, context)
            rows = (
                connection.execute(query.order_by(resources.c.id).limit(limit + 1))
                .mappings()
                .all()
            )
        return {
            "items": [self.present(dict(row)) for row in rows[:limit]],
            "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        }

    def _mutate(self, context, operation, key, payload, correlation_id, action):
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        scope = {
            "provider_id": context.provider_id,
            "principal_id": context.principal_id,
            "operation": operation,
            "idempotency_key": key,
        }
        try:
            with self.engine.begin() as connection:
                self._scope(connection, context)
                if connection.dialect.name == "postgresql":
                    # Serialize only requests sharing this idempotency scope.
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": json.dumps(jsonable_encoder(scope), sort_keys=True)},
                    )
                previous = (
                    connection.execute(select(receipts).filter_by(**scope))
                    .mappings()
                    .first()
                )
                if previous:
                    if previous["request_hash"] != digest:
                        raise APIError(
                            409,
                            "IDEMPOTENCY_KEY_REUSED",
                            "This key was already used with different input.",
                        )
                    return previous["response_status"], previous["response_body"], True
                status, resource, events = action(connection)
                response = self.present(resource)
                timestamp = now()
                connection.execute(
                    insert(audit).values(
                        id=uuid4(),
                        provider_id=context.provider_id,
                        principal_id=context.principal_id,
                        operation=operation,
                        resource_id=resource["id"],
                        resource_version=resource["version"],
                        correlation_id=UUID(correlation_id),
                        outcome=response,
                        created_at=timestamp,
                    )
                )
                for event_type in events:
                    connection.execute(
                        insert(outbox).values(
                            id=uuid4(),
                            provider_id=context.provider_id,
                            event_type=event_type,
                            schema_version=1,
                            resource_id=resource["id"],
                            resource_version=resource["version"],
                            correlation_id=UUID(correlation_id),
                            payload={
                                "resource": response,
                                "integration_mode": "mock",
                                "scope": "development",
                            },
                            created_at=timestamp,
                            published_at=None,
                        )
                    )
                connection.execute(
                    insert(receipts).values(
                        **scope,
                        request_hash=digest,
                        response_status=status,
                        response_body=response,
                        created_at=timestamp,
                    )
                )
                return status, response, False
        except IntegrityError as error:
            code = getattr(error.orig, "sqlstate", None)
            if code == "23505" or "UNIQUE constraint" in str(error.orig):
                raise APIError(
                    409,
                    "DUPLICATE_RESOURCE",
                    "This resource identifier already exists for your provider.",
                ) from error
            raise APIError(
                422, "INVALID_RESOURCE", "Resource violates a database rule."
            ) from error

    def create(self, context, command, key, correlation_id, location, agent):
        payload = {
            "input": command.model_dump(mode="json"),
            "location_outcome": location.outcome,
            "agent_outcome": agent.outcome,
        }

        def action(connection):
            mapped = location.validate(
                command.address, command.latitude, command.longitude
            )
            timestamp = now()
            resource_id = uuid4()
            values = command.model_dump(mode="json")
            values.update(
                **mapped,
                id=resource_id,
                provider_id=context.provider_id,
                created_by=context.principal_id,
                location_validated_at=timestamp,
                availability_observed_at=timestamp
                if command.observed_availability != "unknown"
                else None,
                version=1,
                created_at=timestamp,
                updated_at=timestamp,
                **agent.activate(resource_id, 1),
            )
            row = connection.execute(
                insert(resources).values(**values).returning(resources)
            )
            return (
                201,
                dict(row.mappings().one()),
                [
                    "ResourceRegistered",
                    "AgentActivationRequested",
                ],
            )

        return self._mutate(
            context, "registerResource", key, payload, correlation_id, action
        )

    def update(
        self, context, resource_id, command, key, correlation_id, location, agent
    ):
        payload = {
            "input": command.model_dump(mode="json", exclude_unset=True),
            "location_outcome": location.outcome,
            "agent_outcome": agent.outcome,
        }

        def action(connection):
            current = self._find(connection, context, resource_id)
            if current["version"] != command.expected_version:
                raise APIError(409, "VERSION_CONFLICT", "Reload the latest resource.")
            changes = command.model_dump(
                exclude_unset=True, exclude={"expected_version"}
            )
            fields = set(ResourceBase.model_fields) | {"resource_type", "details"}
            combined = {name: current[name] for name in fields}
            combined.update(changes)
            validated = resource_adapter.validate_python(combined)
            values = validated.model_dump(mode="json")
            if {"address", "latitude", "longitude"} & changes.keys():
                values.update(
                    location.validate(
                        validated.address, validated.latitude, validated.longitude
                    )
                )
                values["location_validated_at"] = now()
            if "observed_availability" in changes:
                values["availability_observed_at"] = (
                    now() if validated.observed_availability != "unknown" else None
                )
            events = ["ResourceUpdated"]
            if {"details", "address", "latitude", "longitude"} & changes.keys():
                values.update(agent.activate(resource_id, current["version"] + 1))
                events.append("AgentActivationRequested")
            if "observed_availability" in changes:
                events.append("ResourceAvailabilityChanged")
            values.update(version=current["version"] + 1, updated_at=now())
            row = (
                connection.execute(
                    update(resources)
                    .where(
                        resources.c.id == resource_id,
                        resources.c.provider_id == context.provider_id,
                        resources.c.version == command.expected_version,
                    )
                    .values(**values)
                    .returning(resources)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise APIError(
                    409, "VERSION_CONFLICT", "Another update won. Reload and retry."
                )
            return 200, dict(row), events

        return self._mutate(
            context,
            f"updateResource:{resource_id}",
            key,
            payload,
            correlation_id,
            action,
        )

    def activate(self, context, resource_id, command, key, correlation_id, agent):
        payload = {"input": command.model_dump(), "agent_outcome": agent.outcome}

        def action(connection):
            current = self._find(connection, context, resource_id)
            if current["version"] != command.expected_version:
                raise APIError(409, "VERSION_CONFLICT", "Reload the latest resource.")
            if current["agent_status"] == "active":
                return 200, current, []
            row = (
                connection.execute(
                    update(resources)
                    .where(
                        resources.c.id == resource_id,
                        resources.c.provider_id == context.provider_id,
                        resources.c.version == command.expected_version,
                    )
                    .values(
                        **agent.activate(resource_id, current["version"]),
                        version=current["version"] + 1,
                        updated_at=now(),
                    )
                    .returning(resources)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise APIError(
                    409, "VERSION_CONFLICT", "Another update won. Reload and retry."
                )
            status = 202 if row["agent_status"] == "pending" else 200
            return status, dict(row), ["AgentActivationRequested"]

        return self._mutate(
            context,
            f"activateAgent:{resource_id}",
            key,
            payload,
            correlation_id,
            action,
        )
