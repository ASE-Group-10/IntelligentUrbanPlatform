# UC01 resource backend

FastAPI application with SQLAlchemy/psycopg persistence in PostgreSQL.
This increment implements UC01 only. UC02 and other component APIs remain future work.

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and an existing migrated database.
From the repository root:

```sh
cd backend
uv sync            # install pinned dependencies from uv.lock
uv run pytest      # tests (contract examples are read from ../contracts/components/*/examples)
uv run ruff check . && uv run ruff format --check .
```

Open <http://127.0.0.1:8000> for the test desk, <http://127.0.0.1:8000/docs> for
interactive API documentation, and `/openapi.json` for the generated specification.
The small HTML/JavaScript test desk is a temporary same-origin harness, not the
planned React product interface. No frontend installation or build is necessary.

The current developer machine is configured in ignored `backend/.env`. For another
developer, start the local database and apply migrations:

```sh
cp .env.example .env
docker compose up -d db
docker compose run --rm dbmate up
```

For shared environments, migrations are run by the database owner from merged
`develop`. Never put a database connection URL in browser code.

## What is real and what is mocked

| Area | Implementation |
| --- | --- |
| Resources | Real records in the configured database; typed validation for all four resource types |
| Transactions | Resource state, successful audit outcome, idempotency receipt, and outbox commit together |
| C02 | Supabase ES256/RS256 access tokens verified against project signing keys. Roles and provider scope come from C02 database records. |
| Mapping | Only three documented synthetic location fixtures; no external map API |
| C08 | Deterministic in-process result; no real agents, negotiation, or allocation |
| C10 | Append-only `c03_audit_outcomes` development stand-in; C10 integration pending |
| Messaging | Unpublished `c03_outbox`; no RabbitMQ consumer/publisher in this increment |

Bind to localhost. This development application is not ready for public
deployment. Setting `MOCK_DEPENDENCIES=false` fails closed until real adapters exist.
The development endpoints and scenario headers must be removed or gated when those
adapters are connected. Existing development outbox records must never be published
as live commands.

## Test the page

1. Enter your development account’s Supabase access token and acting provider UUID, choose a resource type, and inspect its example details.
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

Set `SUPABASE_URL` in the backend environment to the project's HTTPS origin.
Apply the C02 migration and provision a synthetic development user by inserting
their Supabase user UUID and display name into `c02_identity_access.profiles`.
Insert the C03 provider UUID and name into `c02_identity_access.providers`,
then grant the user a provider role in `c02_identity_access.provider_memberships`.
These tables are maintained by the database owner. The API needs read access.

Use the account's access token as `Authorization: Bearer <token>`.
`GET /api/v1/identity/me` returns explicit memberships and the list of
`availableProviders`. A profile with `platform_admin = true` can select any
existing provider, including providers for which it has no membership.
For an admin or multi-provider account, send the selected UUID as `X-Acting-Provider` on
resource calls. To verify UC01, register the synthetic van fixture as a manager,
repeat with a provider outside the user's memberships and check for a 403.
Expired or invalid credentials return 401. Cross-provider resource lookups
under a valid acting context return 404.

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

`database/migrations/20261116160000_create_c03_resource_management.sql` creates the
C03-owned schema, resource tables, constraints, indexes, trigger, and outbox
support. Apply it with dbmate:

```sh
docker compose run --rm dbmate status
docker compose run --rm dbmate up
```

Each migration has a reversible `-- migrate:down` section. No providers, users,
agents, or allocations tables are invented by this module.

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

## Postgres-backed tests

Stores with a Postgres implementation (C06 so far) run the same tests against
the in-memory test double and against a real database. Those tests are skipped
unless `TEST_DATABASE_URL` points at a **dedicated test database**: its schemas
are rebuilt from `database/migrations/` and its tables are emptied before each
test. The test setup refuses any database whose name does not end in `_test`.

Locally, with the Docker Compose database running (`docker compose up -d db`):

```sh
docker compose exec db createdb -U ulop ulop_test   # once
cd backend
TEST_DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop_test uv run pytest
```

CI starts its own Postgres service and sets `TEST_DATABASE_URL` (see
`.github/workflows/backend.yml`).

## RabbitMQ tests

Publisher tests that need a real broker are skipped unless `TEST_RABBITMQ_URL`
is set. They use a throwaway exchange, never the shared `ulop.events`:

```sh
docker compose up -d rabbitmq
TEST_RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F uv run pytest
```

## C06 outbox publisher

Sends queued `CityStateUpdated` events from the C06 outbox to the `ulop.events`
topic exchange (routing key = event type), at least once. Run it as a worker
next to the database and broker:

```sh
DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop \
RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F \
uv run python -m app.components.c06_city_monitoring.publisher
```

Stop it with Ctrl+C. Events stay in the outbox while the broker is down, or
while no consumer queue is bound for their event type yet (for example C06
started before C07 on a fresh broker), and go out once that changes.

## C06 City State API

`GET /api/v1/city-state/snapshot?segmentKeys=seg:4471023,...` returns the
current condition of those segments for the C01 map (`getCitySnapshot` in
`contracts/components/c06-city-monitoring/openapi.yaml`). It needs a C02 bearer
token. Add `runId` to read a simulation run, such as the demo run below, and
`atVersion` for a past version. Segments with no recorded condition are left
out, and values older than `maxAgeSeconds` (default 900) come back `degraded`,
so use `app.demo.feed --fresh` for a closure that shows as current.

## C07 worker

Consumes `CityStateUpdated` from `ulop.events` (queue `c07.city-state-updated`)
and runs `evaluateChange`. Until C05 exists it uses the labelled stub in
`planning_stub.py`, which knows one demo plan on `seg:4471023`.

```sh
RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F \
uv run python -m app.components.c07_disruption_detection.worker
```

Workers log one JSON object per line with `component` and, for events,
`correlationId`, `eventId`, `eventType`, `scope` and `runId`. Following one
`correlationId` across the C06 publisher and the C07 worker shows a closure
going through the whole chain.

Other components can consume events the same way with
`app.shared.events.EventConsumer` (see its docstring). Add your queue and its
event types to `app/shared/topology.py` too: publishers declare those queues at
startup, so your queue keeps events published before your worker starts.

## Demo: road closure through C06 and C07

The thin-slice event chain (#72 hops 7–8), using the shared scenario in
`fixtures/dublin-scenario.json`. With `docker compose up -d db rabbitmq` running
and the migrations applied, start the two workers in separate terminals (see
above), then inject the closure:

```sh
DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop uv run python -m app.demo.feed
```

It prints the run, the `correlationId`, the new City State version and the
queued event ID. The C06 publisher and C07 worker log the same `correlationId`
as the event goes through. Running it again changes nothing; add `--fresh` to
inject a new closure with new IDs at the current time.
