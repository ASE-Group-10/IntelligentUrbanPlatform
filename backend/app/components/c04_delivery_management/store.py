"""SQLAlchemy Core tables for C04 Delivery Management.

Matches ``database/migrations/20261124100000_create_c04_delivery_management.sql``.
Every table is partitioned by ``scope_key`` (``live`` or ``simulation:<runId>``)
so simulation runs stay strictly isolated from live deliveries.
"""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

metadata = MetaData(schema="c04_delivery_management")
json_type = JSON().with_variant(JSONB, "postgresql")

deliveries = Table(
    "deliveries",
    metadata,
    Column("scope_key", Text, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("run_id", Uuid),
    Column("delivery_id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("created_by", Uuid, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("plan_id", Uuid),
    Column("planned_at", DateTime(timezone=True)),
    Column("delivery", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Index(
    "deliveries_scope_provider_idx",
    deliveries.c.scope_key,
    deliveries.c.provider_id,
    deliveries.c.status,
    deliveries.c.delivery_id,
)

command_receipts = Table(
    "command_receipts",
    metadata,
    Column("scope_key", Text, primary_key=True),
    Column("principal_id", Text, primary_key=True),
    Column("operation", Text, primary_key=True),
    Column("idempotency_key", String(200), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

audit_outcomes = Table(
    "audit_outcomes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("scope_key", Text, nullable=False),
    Column("provider_id", Uuid),
    Column("principal_id", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("target_ids", json_type, nullable=False),
    Column("correlation_id", Uuid, nullable=False),
    Column("details", json_type),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

outbox = Table(
    "outbox",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("scope_key", Text, nullable=False),
    Column("delivery_id", Uuid, nullable=False),
    Column("delivery_version", Integer, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("correlation_id", Uuid, nullable=False),
    Column("envelope", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
)


def advisory_lock(connection: Connection, key: str) -> None:
    """Serialize concurrent PostgreSQL transactions sharing one idempotency scope."""
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
