"""C11-owned, durable source observations; no invented metric coefficients."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import UUID4, TypeAdapter
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    delete,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.components.c11_kpi_analytics.contracts import (
    Breakdown,
    Coverage,
    Indicator,
    KPISet,
)
from app.shared.contracts import UtcDatetime
from app.shared.result import Failure, Ok

metadata = MetaData(schema="c11_kpi_analytics")
json_type = JSON().with_variant(JSONB, "postgresql")
observations = Table(
    "observations",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("digest", String(64), nullable=False),
    Column("scope_key", String(50), nullable=False),
    Column("delivery_id", Uuid, nullable=False),
    Column("version", Integer, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("facts", json_type, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)
bindings = Table(
    "delivery_bindings",
    metadata,
    Column("scope_key", String(50), primary_key=True),
    Column("delivery_id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
cursors = Table(
    "page_cursors",
    metadata,
    Column("token", Uuid, primary_key=True),
    Column("binding", String(64), nullable=False),
    Column("offset", Integer, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
)
METRICS = {
    "delivery-time": "s",
    "cost": "EUR",
    "congestion": "1",
    "environmental-impact": "kgCO2e",
}
DEFINITION = "unconfigured-v1"


def scope_key(scope, run_id):
    return "live" if scope == "live" else f"simulation:{run_id}"


def delivery_facts(event):
    """Validate only the consumer's required facts; C04 owns the full schema.

    This private input projection is not a replacement producer DTO. Ownership
    comes only from C04's authoritative providerId, never the UI selection.
    """
    if (
        event.producer != "C04"
        or event.schema_version != 1
        or event.event_type
        not in {"DeliverySubmitted", "DeliveryUpdated", "DeliveryProgressChanged"}
    ):
        raise ValueError("unsupported producer/type/version")
    payload = event.payload
    delivery = payload["delivery"]
    delivery_id = UUID(payload["deliveryId"])
    version = payload["deliveryVersion"]
    if type(version) is not int or version < 1:
        raise ValueError("invalid version")
    if (
        str(delivery_id) != delivery["deliveryId"]
        or delivery["version"] != version
        or delivery["status"] != payload["status"]
    ):
        raise ValueError("inconsistent delivery snapshot")
    if payload["status"] not in {
        "pending",
        "planned",
        "in_progress",
        "delivered",
        "cancelled",
    }:
        raise ValueError("invalid delivery status")
    progress = delivery["progress"]
    if progress["status"] not in {
        "pending",
        "picked_up",
        "in_transit",
        "delivered",
        "failed",
    }:
        raise ValueError("invalid progress status")
    occurred = TypeAdapter(UtcDatetime).validate_python(progress["updatedAt"])
    return (
        delivery_id,
        version,
        occurred,
        {
            "deliveryId": str(delivery_id),
            "deliveryVersion": version,
            "status": delivery["status"],
            "progressStatus": progress["status"],
            "occurredAt": occurred.isoformat(),
            "eventType": event.event_type,
            "sourceEventId": str(event.event_id),
        },
    )


def delivery_provider(event):
    """Missing legacy ownership stays unbound; malformed ownership is rejected."""
    if "providerId" not in event.payload:
        return None
    return TypeAdapter(UUID4).validate_python(event.payload["providerId"])


def bind_on_connection(conn, provider_id, delivery_id, key):
    old = conn.execute(
        select(bindings.c.provider_id).where(
            bindings.c.scope_key == key, bindings.c.delivery_id == delivery_id
        )
    ).scalar_one_or_none()
    if old is not None:
        if old != provider_id:
            raise ValueError("delivery ownership cannot be reassigned")
        return
    conn.execute(
        insert(bindings).values(
            scope_key=key,
            delivery_id=delivery_id,
            provider_id=provider_id,
            created_at=datetime.now(UTC),
        )
    )


class ObservationStore:
    def __init__(self, engine):
        self.engine = engine

    def consume(self, event):
        try:
            delivery_id, version, occurred, facts = delivery_facts(event)
            provider_id = delivery_provider(event)
        except (ValueError, KeyError, TypeError):
            return "reject"
        digest = hashlib.sha256(
            json.dumps(event.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        try:
            with self.engine.begin() as conn:
                old = conn.execute(
                    select(observations.c.digest).where(
                        observations.c.event_id == event.event_id
                    )
                ).scalar_one_or_none()
                if old is not None:
                    if old != digest:
                        return "reject"
                if provider_id is not None:
                    bind_on_connection(
                        conn,
                        provider_id,
                        delivery_id,
                        scope_key(event.scope, event.run_id),
                    )
                if old is not None:
                    return "ack"
                conn.execute(
                    insert(observations).values(
                        event_id=event.event_id,
                        digest=digest,
                        scope_key=scope_key(event.scope, event.run_id),
                        delivery_id=delivery_id,
                        version=version,
                        occurred_at=occurred,
                        facts=facts,
                        received_at=datetime.now(UTC),
                    )
                )
            return "ack"
        except ValueError:
            return "reject"  # conflicting ownership rolls back the whole outcome
        except IntegrityError:
            # Concurrent duplicate: retry after the winning transaction commits.
            return "retry"
        except SQLAlchemyError:
            return "retry"

    def bind_delivery(self, provider_id, delivery_id, scope, run_id):
        """Trusted operator-only mapping, immutable once established.

        No public write endpoint exists. This remains available for trusted legacy
        imports. Current C04 events bind atomically during ingestion.
        """
        key = scope_key(scope, run_id)
        with self.engine.begin() as conn:
            bind_on_connection(conn, provider_id, delivery_id, key)


class ObservationSource:
    def __init__(self, engine, principal_id=None):
        self.engine = engine
        self.principal_id = principal_id

    @staticmethod
    def validate_query(query):
        if query.metric_definition_version != DEFINITION:
            return Failure("Invalid", "Unsupported metricDefinitionVersion")
        if query.scope.area_id is not None or query.scope.resource_ids:
            return Failure("Invalid", "Area/resource source mappings are not available")

    def statement(self, query):
        s = query.scope
        return (
            select(observations.c.event_id, observations.c.facts)
            .join(
                bindings,
                (bindings.c.scope_key == observations.c.scope_key)
                & (bindings.c.delivery_id == observations.c.delivery_id),
            )
            .where(
                bindings.c.provider_id == s.provider_id,
                observations.c.scope_key == scope_key(s.scope, s.run_id),
                observations.c.occurred_at >= s.period.start,
                observations.c.occurred_at < s.period.end,
            )
            .order_by(observations.c.occurred_at, observations.c.event_id)
        )

    def query(self, query):
        error = self.validate_query(query)
        if error:
            return error
        try:
            # Verify the persistent read model is reachable; missing data is not
            # permission to invent a metric value or declare a complete population.
            with self.engine.connect() as conn:
                conn.execute(self.statement(query).limit(1)).first()
            return Ok(
                KPISet(
                    **query.model_dump(),
                    generated_at=datetime.now(UTC),
                    status="partial",
                    indicators=[
                        Indicator(
                            metric_id=metric,
                            unit=unit,
                            value=None,
                            status="unavailable",
                            coverage=Coverage(eligible=0, observed=0),
                            last_updated=None,
                            estimated=False,
                            reason="Metric definition pending; inspect observations",
                        )
                        for metric, unit in METRICS.items()
                    ],
                )
            )
        except SQLAlchemyError:
            return Failure("Unavailable", "C11 read model unavailable")

    def breakdown(self, query, metric_id, limit, cursor):
        error = self.validate_query(query)
        if error:
            return error
        if metric_id not in METRICS:
            return Failure("Invalid", "Unknown metric")
        binding = hashlib.sha256(
            json.dumps(
                {
                    "principal": str(self.principal_id),
                    "query": query.model_dump(mode="json"),
                    "metric": metric_id,
                    "limit": limit,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as conn:
                offset = 0
                as_of = now
                if cursor:
                    try:
                        token = UUID(cursor)
                    except ValueError:
                        return Failure("Invalid", "Invalid cursor")
                    row = (
                        conn.execute(
                            select(cursors).where(
                                cursors.c.token == token,
                                cursors.c.binding == binding,
                                cursors.c.expires_at > now,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        return Failure(
                            "Invalid", "Cursor expired or belongs to another query"
                        )
                    offset = row["offset"]
                    as_of = row["as_of"]
                rows = conn.execute(
                    self.statement(query)
                    .where(
                        observations.c.received_at <= as_of,
                        bindings.c.created_at <= as_of,
                    )
                    .offset(offset)
                    .limit(limit + 1)
                ).all()
                next_cursor = None
                if len(rows) > limit:
                    next_cursor = str(uuid4())
                    conn.execute(delete(cursors).where(cursors.c.expires_at <= now))
                    conn.execute(
                        insert(cursors).values(
                            token=UUID(next_cursor),
                            binding=binding,
                            offset=offset + limit,
                            expires_at=now + timedelta(hours=1),
                            as_of=as_of,
                        )
                    )
                page = rows[:limit]
                return Ok(
                    Breakdown(
                        **query.model_dump(),
                        metric_id=metric_id,
                        records=[row.facts for row in page],
                        source_event_ids=[row.event_id for row in page],
                        next_cursor=next_cursor,
                    )
                )
        except SQLAlchemyError:
            return Failure("Unavailable", "C11 read model unavailable")
