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
repeat; another ID must be allowed in SHARED_RUN_IDS for C11 queries.

Start the existing API/frontend, open `/kpis`, select the same provider, enter the
printed UTC range/run ID and refresh. Observations appear while metrics explicitly
remain unavailable. Load `partial-c04-capture`, open/play/pause/seek/stop and inspect
recorded delivery state. No live delivery/allocation operation is called.

Long-running workers, when running without Compose:

```sh
uv run python -m app.components.c11_kpi_analytics.worker
uv run python -m app.components.c12_simulation_replay.worker
```

## Walking-skeleton hops 9/10 and the shared Compose stack (#72)

`compose.c11-c12.yml` extends the shared stack from **PR #79**. That PR must be
merged or combined in an integration checkout first: the current base stack has
no `api`, `migrate` or backend image. This overlay reuses `ulop-backend:dev`; it
does not duplicate another pair's Dockerfile or replace their services. It adds
C11/C12 workers with the shared event consumer and JSON trace logging. Do not
also run local workers against the same queues.

With #79 available, configure the API's optional `backend/.env` with C02's local
sign-in settings and the database owner's restricted `REPLAY_DATABASE_URL`
(inside Compose, the database hostname is `db`). Set `SHARED_RUN_IDS` to a
JSON array containing the simulation UUID below if it is not already allowed.
The overlay never falls back to a business-writer login for playback, creates
roles, or changes shared/cloud credentials. Then:

```sh
docker compose -f docker-compose.yml -f compose.c11-c12.yml up -d --build
```

For a local hop-9/10 check, set `ANALYTICS_DEMO_TOKEN` in your shell to an existing
C02 analyst bearer token for the selected provider. Keep it out of Git, command
arguments and screenshots. From the repository root, run (replace the provider
placeholder; use a fresh run ID or fresh local DB on repeat):

```sh
docker compose -f docker-compose.yml -f compose.c11-c12.yml run --rm analytics-demo \
  --external-workers --api-url http://api:8000 \
  --provider-id <C02-authorised-provider-UUID> \
  --run-id c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b \
  --correlation-id 835ffac5-9411-4f5f-87bf-a466a84bf820
```

Unlike the default in-process demo, `--external-workers` only publishes; the
separate C11/C12 processes must persist both events within 20 seconds. Missing
or rejected outcomes fail the command without sealing the capture. Both messages
use the supplied `correlationId`, with a causal link between them. The optional
HTTP stage uses the same trace and real C02 token, verifies exact C11 source
event IDs and unavailable metrics, finds the saved partial run, retries session
creation, checks read-only GETs, and plays both original outcomes to completion.
It fails on missing data, denied access, unconfigured replay storage or lost
HTTP correlation IDs. It does not follow redirects or send tokens to remote hosts.
The demo's contract examples are mounted read-only because #79's image does not
bundle `contracts/`.

Without `--api-url`, output explicitly says HTTP verification was not run.
Without `--external-workers`, subscribers run inside the demo process, so that
mode is not evidence that the deployed workers are running.

This remains a **synthetic C04-only component check**, not `scripts/hello-world.sh`
for the entire platform. The #72 coordinator still needs to connect hops 0–8 to
these consumers, agree the event set for the recorded demonstration, and run the
same trace on the demo deployment. C04 production integration/provider binding
requires Julian's review; C01 pacing and access need Julian/Oscar's review;
Compose, migration application and the restricted playback login need the
infrastructure/database owners. A complete KPI formula or polished UI is not a
#72 acceptance condition; fixed behaviour must be labelled. Indicators remain
unconfigured rather than inventing production formulas.

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

### Follow-up to Bruno's storage/worker review

- C11/C12 workers and the synthetic publisher now all read `RABBITMQ_EXCHANGE`,
  defaulting to `ulop.events`, like the other workers. The Compose overlay sets
  the same default explicitly. Custom deployments must configure producers and
  consumers consistently; no message/schema contract changed.
- Integrated #110's C07 router without dropping C11/C12 routers. C11 also uses
  the renamed `shared_run_ids` setting; `SHARED_RUN_IDS` is preferred and the
  central settings model still accepts `C06_SHARED_RUN_IDS` as a legacy alias.
- Both pagination stores already delete expired cursor rows when they create
  a next-page cursor. This is opportunistic cleanup, not a timer; expired rows
  can remain when pagination stops, but they cannot authorise a read. A periodic
  retention job can be added later if volume justifies it. No new scheduler or
  database privilege is needed for the slice.
- Bruno's proposed local-only playback-login setup belongs with the Compose
  integration, outside `database/migrations`. The overlay here is optional and
  can be folded into the shared stack; do not run two copies of each worker.
  The database/Compose owner still needs to supply that setup and its restricted
  login. This PR does not provision credentials or alter Supabase access.
- #112 may correctly report hops 9/10 as `WIRED` until an actual C04 producer
  feeds the agreed demonstration. The synthetic component check does not turn
  the whole-platform result into `PASS`.

Run existing backend pytest/Ruff and frontend lint/test/build commands. Portable
checks use SQLite and explicit trusted C02 context doubles. The same adapters run
against dedicated PostgreSQL in CI, with extra migration rollback/reapply, restricted
SQL-role and RabbitMQ delivery/deduplication tests. A two-consumer trace test sends
both C04 outcomes and duplicates through real RabbitMQ, verifies persistence and
HTTP replay, and checks that C03/C08 data and frozen recordings remain unchanged;
publishing during replay fails the test. Its HTTP identity is an explicit C02
test double, not deployment sign-in evidence. Existing C02 tests cover token
verification. Fixture results are not real provider sign-in evidence.

Compose configuration was validated with Docker Compose 5.8.0 against #79's
`e0518b7a` configuration plus this overlay. This checks service/dependency merging;
it is not an image build or a running-container/deployment demonstration.

Still needed: metric definitions/populations and producer/provider mapping, actual
C04 production events, complete C05/C08/C10 recording and saved explanations,
retention/scaling and final pacing agreement, deployment login/broker ACL proof.
Both #16/#17 stay open. This is an integration increment, not the completed thin slice.
