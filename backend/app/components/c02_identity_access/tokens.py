from uuid import UUID

import jwt
from jwt.exceptions import PyJWKClientConnectionError

from app.infrastructure.errors import APIError


class SupabaseVerifier:
    """Trust only the configured Supabase project's asymmetric signing keys."""

    def __init__(self, project_url: str):
        self.issuer = project_url.rstrip("/") + "/auth/v1"
        self.keys = jwt.PyJWKClient(self.issuer + "/.well-known/jwks.json", timeout=5)

    def subject(self, token: str) -> UUID:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ("ES256", "RS256") or not isinstance(
                header.get("kid"), str
            ):
                raise jwt.InvalidTokenError()
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["ES256", "RS256"],
                issuer=self.issuer,
                audience="authenticated",
                options={"require": ["exp", "sub", "iat"]},
            )
            if claims.get("role") != "authenticated":
                raise jwt.InvalidTokenError()
            return UUID(claims["sub"])
        except PyJWKClientConnectionError as exc:
            raise APIError(
                503, "IDENTITY_UNAVAILABLE", "Identity service unavailable."
            ) from exc
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise APIError(401, "UNAUTHENTICATED", "Sign in to continue.") from exc
