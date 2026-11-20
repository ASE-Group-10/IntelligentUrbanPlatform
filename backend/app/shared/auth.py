"""Verified user and service context shared by protected component operations."""

from typing import Literal
from uuid import UUID

from .contracts import ContractModel
from .result import Failure, Ok, Result

ProviderRole = Literal["manager", "operator", "driver", "analyst", "curator"]
PlatformRole = Literal["admin"]


class ProviderMembership(ContractModel):
    provider_id: UUID
    provider_name: str
    roles: tuple[ProviderRole, ...]


class CurrentIdentity(ContractModel):
    """C02's authenticated user profile, with memberships from trusted storage."""

    principal_id: UUID
    principal_type: Literal["user"] = "user"
    display_name: str
    memberships: tuple[ProviderMembership, ...]
    platform_roles: tuple[PlatformRole, ...]


class AuthContext(ContractModel):
    principal_id: UUID
    principal_type: Literal["user", "service"]
    service_id: str | None = None
    acting_provider_id: UUID | None = None
    roles: tuple[ProviderRole | Literal["service"], ...]
    provider_ids: tuple[UUID, ...]
    platform_roles: tuple[PlatformRole, ...]
    correlation_id: UUID


def resolve_user_context(
    identity: CurrentIdentity,
    acting_provider_id: UUID | None,
    correlation_id: UUID,
) -> Result[AuthContext]:
    """Resolve a requested provider against C02's verified memberships."""
    memberships = {entry.provider_id: entry for entry in identity.memberships}
    if not memberships:
        return Failure("Forbidden", "The caller has no provider access.")
    if acting_provider_id is None:
        if len(memberships) != 1:
            return Failure("Invalid", "X-Acting-Provider is required.")
        acting_provider_id = next(iter(memberships))
    membership = memberships.get(acting_provider_id)
    if membership is None:
        return Failure("Forbidden", "The caller cannot act for this provider.")

    return Ok(
        AuthContext(
            principal_id=identity.principal_id,
            principal_type="user",
            acting_provider_id=acting_provider_id,
            roles=membership.roles,
            provider_ids=tuple(memberships),
            platform_roles=identity.platform_roles,
            correlation_id=correlation_id,
        )
    )


def require_role(
    context: AuthContext, role: ProviderRole, provider_id: UUID | None = None
) -> Result[None]:
    """Check the role for the acting provider, with the documented admin override."""
    target_provider = (
        provider_id if provider_id is not None else context.acting_provider_id
    )
    if context.principal_type == "user" and (
        "admin" in context.platform_roles
        or (
            target_provider is not None
            and target_provider == context.acting_provider_id
            and role in context.roles
        )
    ):
        return Ok(None)
    return Failure("Forbidden", "The caller lacks the required permission.")
