from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.components.c02_identity_access.records import load_identity
from app.infrastructure.errors import APIError, correlation_id
from app.shared.auth import (
    AuthContext,
    CurrentIdentity,
    require_role,
    resolve_user_context,
)
from app.shared.result import Failure

router = APIRouter(prefix="/api/v1/identity", tags=["C02 Identity and Access"])
bearer = HTTPBearer(auto_error=False)


def current_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentIdentity:
    if credentials is None:
        raise APIError(401, "UNAUTHENTICATED", "Sign in to continue.")
    verifier = request.app.state.identity_verifier
    if verifier is None:
        raise APIError(503, "IDENTITY_UNAVAILABLE", "Identity service unavailable.")
    subject = verifier.subject(credentials.credentials)
    return load_identity(request.app.state.engine, subject)


@router.get("/me", response_model=CurrentIdentity, operation_id="getCurrentIdentity")
def get_current_identity(
    identity: Annotated[CurrentIdentity, Depends(current_identity)],
):
    return identity


def authenticate(
    request: Request,
    identity: Annotated[CurrentIdentity, Depends(current_identity)],
    provider: Annotated[UUID | None, Header(alias="X-Acting-Provider")] = None,
) -> AuthContext:
    result = resolve_user_context(identity, provider, UUID(correlation_id(request)))
    if isinstance(result, Failure):
        if result.kind == "Forbidden":
            raise APIError(403, "FORBIDDEN", "Access denied.")
        raise APIError(422, "VALIDATION_FAILED", result.message)
    return result.value


def require_manager(context: AuthContext) -> None:
    if isinstance(require_role(context, "manager"), Failure):
        raise APIError(403, "FORBIDDEN", "Access denied.")
