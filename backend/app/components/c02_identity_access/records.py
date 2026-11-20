"""Read C02-managed profiles, provider memberships and available providers."""

from uuid import UUID

from sqlalchemy import Boolean, Column, MetaData, String, Table, Uuid, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.errors import APIError
from app.shared.auth import CurrentIdentity, ProviderMembership, ProviderSummary

metadata = MetaData(schema="c02_identity_access")
profiles = Table(
    "profiles",
    metadata,
    Column("principal_id", Uuid, primary_key=True),
    Column("display_name", String, nullable=False),
    Column("platform_admin", Boolean, nullable=False),
)
providers = Table(
    "providers",
    metadata,
    Column("provider_id", Uuid, primary_key=True),
    Column("provider_name", String, nullable=False),
)
memberships = Table(
    "provider_memberships",
    metadata,
    Column("principal_id", Uuid, primary_key=True),
    Column("provider_id", Uuid, primary_key=True),
    Column("role", String, primary_key=True),
)


def load_identity(engine: Engine, subject: UUID) -> CurrentIdentity:
    query = (
        select(
            profiles.c.display_name,
            profiles.c.platform_admin,
            providers.c.provider_id,
            providers.c.provider_name,
            memberships.c.role,
        )
        .select_from(
            profiles.outerjoin(
                memberships, profiles.c.principal_id == memberships.c.principal_id
            ).outerjoin(providers, memberships.c.provider_id == providers.c.provider_id)
        )
        .where(profiles.c.principal_id == subject)
        .order_by(providers.c.provider_id, memberships.c.role)
    )
    try:
        with engine.connect() as connection:
            rows = connection.execute(query).all()
            is_admin = bool(rows and rows[0].platform_admin)
            if is_admin:
                available_providers = tuple(
                    ProviderSummary(provider_id=pid, provider_name=name)
                    for pid, name in connection.execute(
                        select(
                            providers.c.provider_id, providers.c.provider_name
                        ).order_by(providers.c.provider_id)
                    )
                )
    except SQLAlchemyError as exc:
        raise APIError(
            503, "IDENTITY_UNAVAILABLE", "Identity records unavailable."
        ) from exc

    grants = {}
    for row in rows:
        if row.provider_id is not None:
            key = (row.provider_id, row.provider_name)
            grants.setdefault(key, []).append(row.role)

    if not is_admin:
        available_providers = tuple(
            ProviderSummary(provider_id=pid, provider_name=name) for pid, name in grants
        )

    return CurrentIdentity(
        principal_id=subject,
        display_name=rows[0].display_name if rows else "",
        memberships=tuple(
            ProviderMembership(provider_id=pid, provider_name=name, roles=roles)
            for (pid, name), roles in grants.items()
        ),
        available_providers=available_providers,
        platform_roles=("admin",) if is_admin else (),
    )
