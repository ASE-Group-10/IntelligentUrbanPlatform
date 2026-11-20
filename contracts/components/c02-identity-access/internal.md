# C02 Identity and Access: internal interfaces

C02 decides who is calling and what they may do. This file covers what other
components use inside the backend process. The HTTP boundary for C01 is in
`openapi.yaml`. Implementation: #56 (backend) and #57 (C01 sign-in).

Typed model: planned in `backend/app/shared/auth.py` (#56). Until then C03 uses
its development stand-in in `backend/app/integrations/identity.py`.
Examples: `examples/`.

## Principles

- **C02 owns identity and memberships.** Roles and provider memberships come
  from C02's records, never from the token's custom claims or the request.
- **Requested scope is not permission.** A `providerId`, `runId` or resource ID
  in a request is a filter. The component checks it against the `AuthContext`.
- **Deny by default.** An operation with no explicit role check is not exposed.
- **401 versus 403.** No valid token (missing, invalid, expired) is `401`
  `UNAUTHENTICATED`. A valid caller without the role or provider is `403`
  `FORBIDDEN`. Neither response reveals whether a restricted record exists.
- **Same error format everywhere.** `{"error": {"code", "message",
  "correlation_id"}}`, as in C03, so C01 needs one error handler.

## Provided interfaces

### authenticate(request) → Result\<AuthContext\>

Callers: every protected HTTP route (as a FastAPI dependency).
Examples: `examples/auth-context-user.json`.

Verifies the bearer token with Supabase Auth, loads the caller's memberships,
and resolves the acting provider. A platform admin explicitly selects any
existing provider using `X-Acting-Provider`. An unknown provider or an empty
provider directory returns `Forbidden`. If providers exist, omitting the
header returns `Invalid`. For all other users the following rules apply.

| Caller's memberships | `X-Acting-Provider` header | Result |
| --- | --- | --- |
| None | any | `Forbidden` (signed in, no access yet) |
| Exactly one | absent | That provider |
| Exactly one | that provider | That provider |
| Any | a provider they belong to | That provider |
| Any | a provider they do not belong to | `Forbidden` |
| More than one | absent | `Invalid`: the header is required |

**Failures:** `Invalid` (missing acting provider), `Forbidden`, and
`Unavailable` (identity provider or records down). A missing or bad token is
reported by the route as `401 UNAUTHENTICATED`.

**AuthContext**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `principalId` | UUID | yes | Supabase Auth user ID, or the fixed ID of a service |
| `principalType` | enum | yes | `user` or `service` |
| `serviceId` | string | service only | Component code of a calling service, for example `C07` |
| `actingProviderId` | UUID | users: yes | Provider resolved from the table above; null for services |
| `roles` | array of enum | yes | The caller's roles **in the acting provider** (`manager`, `operator`, `driver`, `analyst`, `curator`), or `["service"]` |
| `providerIds` | array of UUID | yes | Every provider the caller may select, including all existing providers for a platform admin |
| `platformRoles` | array of enum | yes | `admin`, or empty |
| `correlationId` | UUID | yes | Ties the request to logs, events and decision records |

### service_context(service_id, correlation_id) → AuthContext

Callers: background workers and component-to-component calls that act on
their own behalf, for example C07 calling C05 `requestReplan`.
Example: `examples/auth-context-service.json`.

Returns a service identity (`principalType: service`, `roles: ["service"]`).
A service context is only created inside the backend, never from an HTTP
request.

### require_role(context, role, provider_id=None) → Result\<None\>

Callers: any component operation that needs a role.

The target must match the acting provider. Within it, either the required
provider role or the `admin` platform role grants access.

## Required interfaces

- Supabase Auth: verifies access tokens (signing keys from the project's JWKS
  endpoint). No other component talks to Supabase Auth directly.

## Development identities

Until #56 lands, C03's stand-in development tokens (`dev-manager`,
`dev-operator`, `dev-other-provider`) map onto the same fields: one provider
membership each. Tests should build `AuthContext` values directly rather than
use real tokens.

## Still open

- [ ] Where membership records live: proposed `c02_identity_access.provider_memberships` (migration in #56).
- [ ] Session lifetime and refresh behaviour in C01 (#57).
- [ ] JSON field style: C02 and C03 errors use `correlation_id` (snake_case); C06, C11 and C12 bodies use camelCase. Agree one style for new contracts.
