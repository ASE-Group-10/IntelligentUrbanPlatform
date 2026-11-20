from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.infrastructure.errors import APIError


@dataclass(frozen=True)
class AuthContext:
    principal_id: UUID
    provider_id: UUID
    role: str


# Synthetic development identities, never Supabase keys or real user tokens.
PROVIDER_A = UUID("10000000-0000-4000-8000-000000000001")
PROVIDER_B = UUID("10000000-0000-4000-8000-000000000002")
IDENTITIES = {
    "dev-manager": AuthContext(
        UUID("20000000-0000-4000-8000-000000000001"), PROVIDER_A, "manager"
    ),
    "dev-operator": AuthContext(
        UUID("20000000-0000-4000-8000-000000000002"), PROVIDER_A, "operator"
    ),
    "dev-other-provider": AuthContext(
        UUID("20000000-0000-4000-8000-000000000003"), PROVIDER_B, "manager"
    ),
}
bearer = HTTPBearer(auto_error=False)


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if not request.app.state.settings.mock_dependencies:
        raise APIError(503, "IDENTITY_UNAVAILABLE", "C02 is not connected yet.")
    identity = IDENTITIES.get(credentials.credentials) if credentials else None
    if identity is None:
        raise APIError(401, "UNAUTHENTICATED", "A valid development token is required.")
    return identity


def require_manager(context: AuthContext):
    if context.role != "manager":
        raise APIError(403, "FORBIDDEN", "Resource changes require a resource manager.")
