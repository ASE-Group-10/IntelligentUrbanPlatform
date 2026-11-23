# C11/C12 integration — #16 and #17

Jolin, AI-assisted. Supersedes process-only prototypes #66/#67. Outside review
required from C04/C08/C06 and C02/database owners. No paired session or handover
is claimed. No shared Supabase changes are applied by this PR.

## Implemented

- C11's durable RabbitMQ queue receives C04 version-1 delivery outcomes, persists
  allowlisted observations in its owned schema and deduplicates event IDs atomically.
  Conflicting duplicate IDs are rejected. C04 supplies no authoritative provider
  field: observations are invisible until a trusted operator binds delivery/scope
  ownership. Bindings cannot be reassigned; there is no public binding/write API.
- Existing C02 identity and analyst/provider checks protect every endpoint. C11
  simulation queries use C06's configured shared run IDs. Unsupported area/resource
  filters are rejected. The runtime metric definition is `unconfigured-v1`: values
  are null/unavailable until populations/formulas/rates are agreed. Coverage 0/0
  means no metric population is defined, not a count of received events. Breakdown
  contains source facts, not calculated KPI contributions. No coefficients invented.
- Opaque breakdown cursors bind principal, query, metric and limit, expire after
  one hour and retain an ingestion/binding cutoff. Query refresh can use a bounded
  process-local stale cache on read failure; restart loses fallback, not source data.
- C12 captures only explicitly registered simulation runs. Full original C04
  events and explicit initial configuration persist before ack. Duplicates are
  idempotent. Conflicts, backwards observed time and overflow invalidate capture.
  Sealing freezes ordered snapshots with initial-state and configuration hashes.
- C12 recordings currently cover **C04 only**, remain `incomplete`, and state that
  limitation on playback. They do not demonstrate a complete simulation engine or
  C05/C08/C10 recording. Replay never calls a planner, current KPI query or broker.
- Provider-scoped run listing/filtering/paging, persistent private sessions,
  idempotent controls, row-locked cursor checks and explicit advance ticks execute
  through HTTP. GET polling is read-only. Sessions/receipts survive API restarts.
  `/kpis` has authenticated source observations and saved-run replay controls;
  presentation pacing is one explicit tick/second.

## Local setup and demonstration

Use a fresh **local** PostgreSQL/RabbitMQ project, existing dbmate migration flow,
and existing locked dependencies. The two new up/down migrations touch only C11
and C12 schemas. Shared migrations are applied by the database owner after review.

Backend uses its existing DATABASE_URL, RABBITMQ_URL and C02/Supabase settings.
Sign in with a user holding the provider's analyst role (or existing admin override).
This change grants no user roles. A separate `REPLAY_DATABASE_URL` is required;
replay fails closed if unset. The database owner provisions a restricted login
outside Git. Its group role (here named c12_playback) needs only:

```sql
GRANT USAGE ON SCHEMA c12_simulation_replay TO c12_playback;
GRANT SELECT ON c12_simulation_replay.runs TO c12_playback;
GRANT SELECT, INSERT, UPDATE ON c12_simulation_replay.sessions,
    c12_simulation_replay.command_receipts TO c12_playback;
GRANT SELECT, INSERT, DELETE ON c12_simulation_replay.run_page_cursors TO c12_playback;
```

Do not reuse a migration/service-owner login or inherit business-write roles.
The PostgreSQL test uses SET LOCAL ROLE to prove denied recording/C03/C08 writes.
Deployment must still verify its actual login and broker/network isolation.

From `backend/`, after migrations and local services are ready:

```sh
uv run python -m app.components.analytics_demo \
  --provider-id <C02-authorised-provider-UUID> \
  --run-id c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b
```

This refuses remote services. It publishes synthetic C04 contract examples through
RabbitMQ, runs actual C11/C12 handlers and seals a labelled C04-only capture. It
does not exercise a real C04 producer. Use a fresh local DB or a new run ID on
repeat; another ID must be allowed in C06_SHARED_RUN_IDS for C11 queries.

Start the existing API/frontend, open `/kpis`, select the same provider, enter the
printed UTC range/run ID and refresh. Observations appear while metrics explicitly
remain unavailable. Load `partial-c04-capture`, open/play/pause/seek/stop and inspect
recorded delivery state. No live delivery/allocation operation is called.

Long-running workers (started separately; pending Compose PR #79 is unchanged):

```sh
uv run python -m app.components.c11_kpi_analytics.worker
uv run python -m app.components.c12_simulation_replay.worker
```

Trusted operator commands, before/after the feed:

```sh
uv run python -m app.components.analytics_admin begin-capture \
  --provider-id <provider> --run-id <run> --start <UTC-time> \
  --configuration <configuration.json> --synthetic
uv run python -m app.components.analytics_admin bind-delivery \
  --provider-id <provider> --delivery-id <delivery> --run-id <run>
uv run python -m app.components.analytics_admin seal-capture \
  --run-id <run> --end <UTC-time-after-last-event>
```

Configuration explicitly includes initialState, e.g.
`{"initialState":{"deliveries":{}},"scenario":"synthetic-C04-only"}` for a new
empty synthetic run. Never assume an existing run has empty initial state.
Unregistered runs are intentionally not captured; C12 rejects live input.
`import-archive <file>` accepts a verified archive (configuration digest, checkpoint
and ranges; max 50 MB / 10000 outcomes) and never overwrites a saved run.

## Verification and remaining work

Run existing backend pytest/Ruff and frontend lint/test/build commands. Portable
checks use SQLite and explicit trusted C02 context doubles. The same adapters run
against dedicated PostgreSQL in CI, with extra migration rollback/reapply, restricted
SQL-role and RabbitMQ delivery/deduplication tests. Existing C02 tests cover token
verification. Fixture results are not real provider sign-in evidence.

Still needed: metric definitions/populations and producer/provider mapping, actual
C04 production events, complete C05/C08/C10 recording and saved explanations,
retention/scaling and final pacing agreement, deployment login/broker ACL proof.
Both #16/#17 stay open. This is an integration increment, not the completed thin slice.
