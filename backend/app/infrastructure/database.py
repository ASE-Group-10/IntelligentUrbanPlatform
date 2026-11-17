from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    Uuid,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
json_type = JSON().with_variant(JSONB, "postgresql")
resources = Table(
    "resources",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("external_identifier", String(100), nullable=False),
    Column("name", String(200), nullable=False),
    Column("resource_type", String(20), nullable=False),
    Column("address", Text, nullable=False),
    Column("latitude", Numeric(9, 6), nullable=False),
    Column("longitude", Numeric(9, 6), nullable=False),
    Column("location_reference", Text),
    Column("location_validated_at", DateTime(timezone=True), nullable=False),
    Column("details", json_type, nullable=False),
    Column("profile_status", String(20), nullable=False),
    Column("observed_availability", String(20), nullable=False),
    Column("availability_observed_at", DateTime(timezone=True)),
    Column("agent_status", String(20), nullable=False),
    Column("agent_id", Uuid),
    Column("version", Integer, nullable=False),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index(
    "resources_provider_external_identifier_uidx",
    resources.c.provider_id,
    func.lower(func.trim(resources.c.external_identifier)),
    unique=True,
)
receipts = Table(
    "c03_command_receipts",
    metadata,
    Column("provider_id", Uuid, primary_key=True),
    Column("principal_id", Uuid, primary_key=True),
    Column("operation", Text, primary_key=True),
    Column("idempotency_key", String(200), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
audit = Table(
    "c03_audit_outcomes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("principal_id", Uuid, nullable=False),
    Column("operation", Text, nullable=False),
    Column("resource_id", Uuid, nullable=False),
    Column("resource_version", Integer, nullable=False),
    Column("correlation_id", Uuid, nullable=False),
    Column("outcome", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
outbox = Table(
    "c03_outbox",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("provider_id", Uuid, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("resource_id", Uuid, nullable=False),
    Column("resource_version", Integer, nullable=False),
    Column("correlation_id", Uuid, nullable=False),
    Column("payload", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
)


def make_engine(url: str):
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if url.startswith("postgresql"):
        options["connect_args"] = {
            "connect_timeout": 10,
            "prepare_threshold": None,
            "options": "-c search_path=c03_resource_management,public",
        }
    return create_engine(url, **options)
