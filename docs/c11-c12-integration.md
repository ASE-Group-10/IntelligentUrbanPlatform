# C11/C12 integration — #16, #17 and #72

Jolin, AI-assisted. Supersedes #66/#67. No completed paired session or handover
is claimed. Outside review remains required. No shared Supabase migration or
credential change has been applied.

## Current behaviour

- C11/C12 reuse the shared durable RabbitMQ consumer, event-ID deduplication and
  JSON trace logging. Both workers and the synthetic publisher honour
  `RABBITMQ_EXCHANGE`, defaulting to `ulop.events`.
- C11 validates its C04 input projection. With #126's authoritative
  `payload.providerId`, binding and observation commit atomically. Ownership is
  immutable: a conflicting provider is rejected without saving that outcome.
  Legacy messages without providerId stay private until a trusted operator binds
  them; malformed provider IDs are rejected. No public binding/write endpoint exists.
- C02 verifies analyst/provider access on every HTTP operation. Simulation reads
  use the central `SHARED_RUN_IDS` setting (`C06_SHARED_RUN_IDS` is a legacy alias).
  Opaque cursors bind principal, filters and snapshot; unsupported source filters
  are rejected. Both pagination stores opportunistically delete expired cursors
  when creating a next-page cursor; periodic retention can come later.
- C11's metric version is `unconfigured-v1`: indicators are null/unavailable until
  formulas and populations are agreed. Breakdown is source observations, not
  invented metric contributions. Coverage 0/0 means the population is undefined.
- C12 records only explicitly registered simulation runs. New capture requires
  authoritative C04 providerId and ignores other providers' outcomes in a shared
  run. It stores original outcomes/configuration before ack, deduplicates retries,
  and rejects conflicting history, backwards timestamps and overflow.
- Sealing freezes initial state/configuration hashes and ordered snapshots. Current
  recording covers **C04 delivery outcomes only**, is labelled `incomplete`, and
  does not claim full C05/C08/C10 history or saved decision explanations.
- Provider-scoped run listing and owner-bound replay sessions/receipts persist in
  PostgreSQL. Controls and explicit advance ticks check cursors transactionally.
  GET polling is read-only. Playback uses a separate restricted SQL connection,
  does not call a planner, current KPI source or publisher, and fails closed if
  `REPLAY_DATABASE_URL` is absent. `/kpis` displays observations and replay controls.

## One-command local Compose setup

After this PR is merged, from the repository root:

```sh
docker compose up -d --build
```

The root stack now includes `c11-worker`, `c12-worker` and `c12-local-setup`.
The setup waits for local migrations and runs in `network_mode: service:db`.
Its entry point requires `C12_LOCAL_COMPOSE=1` and always connects to the fixed
loopback `ulop` database inside that namespace. It ignores `DATABASE_URL`,
`MIGRATION_DATABASE_URL` and `backend/.env`; it cannot target Supabase via those
settings. It creates/updates the local-only `c12_playback` login with public,
disposable Compose credentials, outside `database/migrations`.

The login has SELECT on saved runs; SELECT/INSERT/UPDATE on sessions and command
receipts; SELECT/INSERT/DELETE on run-page cursors. It cannot read capture buffers,
write recordings or operational tables, create component objects, inherit roles
or act as an administrator. Setup refuses an existing broader role rather than
silently accepting it. If local state already has an incompatible role, review
that local role or use a fresh disposable local stack; do not change shared roles.
The API waits for setup and receives a local `REPLAY_DATABASE_URL` for this login.
C11/C12 workers retain the recording/read-model connection, not the playback login.

Configure C02's normal local Supabase sign-in settings in optional `backend/.env`.
An actual user still needs the provider's analyst role; setup grants no application
roles and does not bypass identity checks. Do not run duplicate host workers on
these same queues. With a container rebuild, any changed migration requires the
normal reviewed local migration flow.

For a shared/deployed database, the database owner must separately provision a
restricted login and set `REPLAY_DATABASE_URL` in that environment. Local Compose
credentials/setup are not a cloud deployment configuration. The grants required
on the C12 schema are:

```sql
GRANT USAGE ON SCHEMA c12_simulation_replay TO c12_playback;
GRANT SELECT ON c12_simulation_replay.runs TO c12_playback;
GRANT SELECT, INSERT, UPDATE ON c12_simulation_replay.sessions,
    c12_simulation_replay.command_receipts TO c12_playback;
GRANT SELECT, INSERT, DELETE ON c12_simulation_replay.run_page_cursors TO c12_playback;
```

## Synthetic component check over real local connections

`compose.c11-c12.yml` is now only an optional synthetic demo service; it does not
start a second set of workers. First start the root stack as above. Set
`ANALYTICS_DEMO_TOKEN` in your shell to an existing C02 analyst token for the
chosen provider; keep it out of Git, command arguments and screenshots. Then:

```sh
docker compose -f docker-compose.yml -f compose.c11-c12.yml run --rm analytics-demo \
  --external-workers --api-url http://api:8000 \
  --provider-id <C02-authorised-provider-UUID> \
  --run-id c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b \
  --correlation-id 835ffac5-9411-4f5f-87bf-a466a84bf820
```

Use a fresh local run/database on repeat. Any replacement run must be allowed in
`SHARED_RUN_IDS`. The demo overrides fixture providerId with the supplied provider,
registers an explicitly synthetic capture, publishes two C04 examples with one
trace and causal chain, and waits up to 20 seconds for both external workers.
Missing/rejected outcomes fail the command without sealing. No manual C11 binding
is needed for these authoritative fixtures.

The HTTP stage checks exact source event IDs, unavailable indicators, saved partial
run discovery, idempotent session creation, read-only GETs and playback of both
original outcomes through completion. It preserves the same correlationId and fails
on missing data, denied access or unavailable replay storage. Tokens stay on allowed
local origins; redirects are not followed. Contract examples are mounted read-only
because the shared backend image does not bundle `contracts/`.

Without `--api-url`, output explicitly says HTTP was not verified. Without
`--external-workers`, the demo runs its subscribers in-process and is not proof
that separately deployed workers are running. Neither mode is the full-platform
hello-world run. This remains a labelled **synthetic C04-only component check**.

## Capturing a real C04 demo run

C04's actual outbox publisher and authoritative provider field are supplied by
**PR #126 / Issue #124**. They are not duplicated or merged into this PR. Until that
producer is available, the whole-platform script can correctly report hops 9/10
as `WIRED`, not `PASS`.

Register the capture **before** submitting the delivery. For a new empty run, an
explicit configuration file can contain
`{"initialState":{"deliveries":{}},"scenario":"Dublin-C04"}`. For an existing
run, supply its actual initial state owned by the same provider, never assume empty.
From `backend/` (or through the API container), use the trusted operator commands:

```sh
uv run python -m app.components.analytics_admin begin-capture \
  --provider-id <provider> --run-id <run> --start <UTC-before-first-event> \
  --configuration <configuration.json>
# Submit the real delivery through C01/C04; do not pass --synthetic above.
uv run python -m app.components.analytics_admin seal-capture \
  --run-id <run> --end <UTC-after-last-event>
```

C11 binds automatically from C04 providerId. C12 only captures the registered run
and provider. Live inputs are never recorded. Open `/kpis` with the same analyst,
provider, UTC range and run ID to inspect observations and saved playback. Current
archives are still C04-only and incomplete even when these outcomes are real.
Trusted `bind-delivery` remains for legacy C11 imports. `import-archive` accepts a
verified archive (configuration digest/checkpoints/ranges, max 50 MB and 10000
outcomes) and never overwrites an existing run.

## Verification and remaining coordination

The existing backend test suite runs portable checks locally and PostgreSQL/
RabbitMQ checks on dedicated CI services. The two-consumer test uses actual worker
factories on a custom exchange, with duplicate delivery. It verifies atomic binding,
recording and HTTP playback over a **real restricted SQL login** provisioned twice
by the same helper as Compose. It checks unchanged operational/frozen tables,
rejects recording/business writes and unexpected grants, and fails if playback
publishes. HTTP identity in this test is an explicitly trusted C02 double, not
actual sign-in/deployment evidence.

The latest develop merge retains all C04/C05/C07/C11/C12 HTTP routers. Root Compose
and the optional demo overlay are configuration-checked; an image/container startup
or deployed browser walkthrough is not claimed unless separately demonstrated.
The C11/C12 owned up/down migrations are unchanged and still require review before
the database owner applies them to shared Supabase.

Remaining work: Julian/Oscar review of mapping, pacing and access; #126 producer
integration; the #72 agreed event set and same-trace live walkthrough; demo-deployment
login and broker isolation proof; complete C05/C08/C10 histories and metric agreements
for later full functionality. The thin slice permits labelled simple logic and does
not require polished UI or full KPI formulas. Both #16/#17 remain open.

## Complete local connection check (integrated #126)

The integration branch includes merged #126's real C04 outbox publisher. The
existing backend workflow now also boots the whole local Compose stack, including
the actual API, publishers, workers, PostgreSQL, RabbitMQ and local playback setup.
`compose.walking-skeleton.yml` adds an explicitly test-only HTTPS/JWKS issuer:
its keys/token are generated per run and never use personal/Supabase credentials.
The normal API validates TLS, JWT signature/issuer/audience/expiry and C02 database
memberships; no authentication dependency or consumer handler is replaced.
This verifies authentication wiring, not the hosted Supabase sign-in screen.

The check loads the existing synthetic C09 corpus and creates isolated C02 test
memberships, then uses `app.demo.analytics_trace run` to register a capture and
submit a synthetic delivery through the real C04 HTTP route. Only the actual
C04 outbox publisher delivers it to RabbitMQ; only the independent C11/C12 workers
ingest it. It waits for broker confirmation and both persistent stores, seals
the C04-only recording, and verifies authorised observations and replay through
the API's actual restricted playback login. Operational/frozen rows must remain
unchanged after playback. No direct consumer calls or fixture event publication
can stand in for those connections.

`scripts/hello-world.sh --analytics-only --run-id <run> --correlation-id <trace>`
rechecks an already sealed local run with `ULOP_TOKEN` and
`ULOP_ACTING_PROVIDER` set. The same verification is also used by the full script.
Routes/consumers alone stay WIRED. Missing or mismatched trace evidence cannot
produce PASS. The selected-mode result covers only hops 9/10, explicitly not the
whole-platform result. The CI check also proves a missing run and a stopped C11
consumer exit unsuccessfully.

For a disposable local reproduction, generate fixture keys with
`ULOP_TEST_IDENTITY=1 uv run --project backend python scripts/compose_identity.py
prepare --folder .tmp/slice-auth`, then use the two Compose files shown in the
workflow. Run the seed/capture/check steps exactly as in
`.github/workflows/backend.yml`'s `compose-smoke` job. Normal root Compose does
not use this issuer. Test keys, tokens and local volumes are excluded from Git;
the evidence artifact contains only the trace proof, selected-hop report,
service statuses and service logs. It never uploads the issuer directory.

The workflow uses its own Compose project and removes only that disposable
project at the end. A green result is local container/HTTP/RabbitMQ/storage
evidence with synthetic business input and a temporary test identity. Full-platform
recording, hosted sign-in, the visual browser walkthrough and deployed networking
remain separately verifiable work.
