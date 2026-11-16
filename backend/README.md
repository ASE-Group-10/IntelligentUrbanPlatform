# UC01 resource backend

FastAPI application with SQLAlchemy/psycopg persistence in Supabase PostgreSQL.
This increment implements UC01 only. UC02 and other component APIs remain future work.

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and an existing migrated database.
From the repository root:

```sh
uv sync --project backend --locked
uv run --directory backend uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open <http://127.0.0.1:8000> for the test desk, <http://127.0.0.1:8000/docs> for
interactive API documentation, and `/openapi.json` for the generated specification.
The small HTML/JavaScript test desk is a temporary same-origin harness, not the
planned React product interface. No frontend installation or build is necessary.

The current developer machine is configured in ignored `backend/.env`. For another
developer, use `backend/.env.example` with a restricted PostgreSQL connection, or:

```sh
supabase link gfjqwlkclokbycdantrl
uv run --project backend python scripts/configure_local_database.py
```

The setup script creates a separate login inheriting the restricted `c03_backend`
role and saves a generated password in `backend/.env` with owner-only permissions.
It never changes existing passwords or overwrites existing configuration. Coordinate
shared database changes with the team. Never put the connection URL in browser code.

## What is real and what is mocked

| Area | Implementation |
| --- | --- |
| Resources | Real records in the configured database; typed validation for all four resource types |
| Transactions | Resource state, successful audit outcome, idempotency receipt, and outbox commit together |
| C02 | Synthetic bearer identities, provider scope, and role checks; no real sign-in |
| Mapping | Only three documented synthetic location fixtures; no external map API |
| C08 | Deterministic in-process result; no real agents, negotiation, or allocation |
| C10 | Append-only `c03_audit_outcomes` development stand-in; C10 integration pending |
| Messaging | Unpublished `c03_outbox`; no RabbitMQ consumer/publisher in this increment |

Bind to localhost. This mock-authenticated application is not ready for public
deployment. Setting `MOCK_DEPENDENCIES=false` fails closed until real adapters exist.
The development endpoints and scenario headers must be removed or gated when those
adapters are connected. Existing development outbox records must never be published
as live commands.

## Test the page

1. Keep **Provider A · Manager**, choose a resource type, and inspect its example details.
2. Register a resource with a unique identifier, then change its name and save.
3. Click **Repeat last mutation (same key)**. The response is replayed without a write.
4. Create another resource with **Agent activation → Fails**. It remains stored and ineligible.
5. Change activation to **Succeeds** and click **Activate agent**.
6. Choose **Location validation → Service unavailable** for a new registration.
   It returns 503 and stores nothing. There is no pending-review flow.
7. Switch to **Provider B** to check isolation, or **Operator** to test denied writes.

Synthetic fixtures:

| Address | Latitude | Longitude |
| --- | --- | --- |
| Dublin Depot (synthetic) | 53.3498 | -6.2603 |
| Docklands Hub (synthetic) | 53.3480 | -6.2380 |
| City Centre Locker (synthetic) | 53.3438 | -6.2546 |

## API contracts

All resource endpoints require `Authorization: Bearer <development-token>`.
The tokens are `dev-manager`, `dev-operator`, and `dev-other-provider`; these are
public synthetic test identities, not real authentication credentials. The manager
tokens may write only their own provider's resources. Operators may read Provider A.
Cross-provider lookups return 404.

All mutations require `Idempotency-Key` (1–200 characters: letters, digits, `. _ : -`).
Keep the same key and body on transport retries. A completed retry returns the
original response snapshot and `Idempotency-Replayed: true`, even if the record has
subsequently changed; use GET for current state. A different body or mock scenario
with the same key returns 409. Failed validation can be corrected and resubmitted.

| Method | Path | Success |
| --- | --- | --- |
| POST | `/api/v1/resources` | 201, resource and actual mock activation outcome |
| GET | `/api/v1/resources` | 200, `{items, next_cursor}` |
| GET | `/api/v1/resources/{id}` | 200, current resource |
| POST | `/api/v1/resources/{id}` | 200, updated resource |
| POST | `/api/v1/resources/{id}/agent-activation` | 200 completed outcome, 202 pending |

List filters: `resource_type`, `profile_status`, `limit` (1–100, default 20), and
`cursor` (UUID from the previous page). Ordering is by resource UUID. New inserts
may appear before the current cursor; refresh to see them. Pagination is not a snapshot.

Create example:

```json
{
  "external_identifier": "VAN-001",
  "name": "Example electric van",
  "resource_type": "vehicle",
  "address": "Dublin Depot (synthetic)",
  "latitude": 53.3498,
  "longitude": -6.2603,
  "observed_availability": "available",
  "details": {
    "vehicle_category": "van",
    "max_weight_kg": 800,
    "powertrain": "electric",
    "autonomous": false,
    "timezone": "Europe/Dublin",
    "operating_hours": [],
    "restrictions": []
  }
}
```

Update example: `{"expected_version": 1, "name": "Updated van"}`.
Activation body: `{"expected_version": 2}`. Existing active agents are reused
without incrementing the resource version. Failed activation returns the stored
resource with `agent_status=failed`, not a false successful activation.

Provider/principal IDs, agent state, location-validation metadata, observation
timestamps, record timestamps, and version are server controlled. Resource type is
immutable in this increment. Supplied `details` replace the whole object and must
pass that resource type's schema. Operating hours, timezone, and restrictions are
nested in `details`, matching the current live table. Empty hours mean unspecified.
The supported restriction types are `temperature_range` and `operating_zone`;
they are preserved as requirements, not enforced by a planner in this increment.

Mock scenario headers:

```text
X-Mock-Location-Result: matched | unmatched | unconfirmed
X-Mock-Agent-Result: active | failed | pending
```

Errors use `{ "error": { "code", "message", "correlation_id", "fields"? } }`.
Expected statuses: 401 authentication, 403 role denied, 404 absent/out of scope,
409 duplicate/version/key conflict, 422 validation/unmatched location,
503 unavailable dependency or unconfirmed database outcome. On an uncertain write,
retry with the same key. Every response includes `X-Correlation-ID`.

`planning_eligible` is calculated, not stored. It requires an active profile, active
agent, available observation, and an observation no older than the configured
freshness threshold (development default: 24 hours). This is a proposed C05/C08
contract and does not reserve capacity or imply confirmed allocation.

## Database and migrations

`supabase/migrations/20261116160000_resources.sql` captures the existing table,
including automatic version/timestamp updates. The next migration adds C03 receipt,
audit, and outbox tables and a non-login backend role. A transaction-local provider
setting restricts this role through row-level security. The backend derives that
setting from authenticated context; it is never accepted from request bodies.
The login is trusted backend infrastructure and must not be distributed to browsers.

For a new local Supabase environment, run `supabase start` and `supabase db push`.
For hosted migrations, review the target and pending migrations first:

```sh
supabase migration list --remote
supabase db push --remote --dry-run
supabase db push --remote
```

Do not replay the baseline against a database where the table already exists;
reconcile its migration history with the team first. No providers, users, agents,
or allocations tables are invented by this module.

## Verification

```sh
uv run --directory backend pytest
uv run --project backend ruff check --config backend/pyproject.toml backend scripts
uv run --project backend ruff format --check --config backend/pyproject.toml backend scripts
node --check backend/app/static/app.js
```

Routine tests use isolated SQLite databases and mock adapters; they do not contact
the shared Supabase project. They cover four types, invalid details/locations,
provider/role checks, duplicates, receipt replay, stale versions, activation failure
and recovery, and pagination. Actual Supabase/browser verification is recorded in
`docs/uc01-verification.md`; SQLite alone does not prove PostgreSQL policies/triggers.
