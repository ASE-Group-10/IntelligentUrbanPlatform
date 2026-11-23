# C12 Simulation and Replay: internal interfaces (UC12)

Current integration increment (#16/#17): HTTP routing, C02 analyst/provider checks,
owned PostgreSQL storage and C04 receive workers are implemented. See the
[executable integration guide](../../../docs/c11-c12-integration.md) for commands,
recording coverage and outstanding agreements. This supersedes the original
proposal's no-runtime/no-database statements below. No completed paired session or
full thin-slice integration is claimed. C11 values remain unavailable until metric
formulas are agreed; C12 C04-only captures remain explicitly incomplete.


Original contract proposal (historical context; current runtime noted above). Follows #35/#46;
HTTP publication is tracked by #48. Pair: Jolin + Qingyuan. Requested HTTP reviewers:
Julian (Antel0p3) and Oscar (OscarGomezGonzalezz). AI-assisted preparation for Jolin;
no completed paired work or UC12 handover is claimed.

The C01-facing API definitions, field schemas, examples and HTTP failures now live
in [openapi.yaml](openapi.yaml). Browser calls are not internal interfaces.
Transport proposals (paths, paging, idempotency receipts and error envelopes)
need consumer review. In-process `Result<T>`, C02 context and typed models remain
unchanged; HTTP adapters translate them rather than redefining shared types.
Event contracts stay in [events.yaml](events.yaml).

## C01 HTTP operations

| Logical operation | HTTP contract |
| --- | --- |
| startSimulation | POST `/api/v1/simulation-runs` |
| listRuns | GET `/api/v1/simulation-runs` |
| startReplay | POST `/api/v1/replay-sessions` |
| getReplayFrame (read-only polling) | GET `/api/v1/replay-sessions/{sessionId}` |
| controlReplay | POST `/api/v1/replay-sessions/{sessionId}/controls` |

The OpenAPI file defines authentication, fields, status mapping, timeline controls,
idempotent HTTP retries and transport-only run paging. Session ID comes from the
control path, then the adapter supplies it to the existing ReplayControl model.
State/range/cursor validation remains required in the service implementation.
Existing typed value models remain in
`backend/app/components/c12_simulation_replay/contracts.py`.

## Provided internal interfaces

### recordSimulationEvent(runId, record: RecordingRecord) → Result<recording receipt>

Caller: trusted isolated simulation recorder, never a public replay session.
`runId` is a required UUID v4 matching record and event. Example:
[record](examples/recording-record.json). Its `SyntheticDeliveryCompleted` event
from `C12-fixture` is deliberately synthetic, not a production event declaration.
Real payloads use the original producer's schema; see [events.yaml](events.yaml).

Persist full ordered outcomes independent of coalesced city notifications.
Sequence is contiguous/one-based per run; same eventId returns original receipt,
conflicting ID/sequence fails. **Receipt DTO fields remain unagreed**; the model
does not enforce cross-record ordering/durability. Failures: `Invalid` (wrong
run/scope/payload), `Forbidden`, `Conflict`, `Unavailable` (no partial recording).

### advanceReplay(sessionId, expectedCursor) → Result<ReplayFrame>

Caller: trusted replay scheduler. `sessionId`: UUID v4, required; `expectedCursor`:
nonnegative strict integer, required. Applies the next recorded outcome exactly
once; completed at range end. Failures: `Invalid`, `Forbidden`, `NotFound`,
`Conflict`, `Unavailable`. Interrupted recording reads pause without cursor advance.
This is a provided scheduler boundary; checkpoint loading/reconstruction helpers
are component implementation details and are not additional public contracts.

## Internal recording/checkpoint values

`RecordingRecord` (required fields): runId (UUID v4), sequence (strict integer >=1),
simulationTime (UTC timestamp), event (shared EventEnvelope from the same simulation
run). Example: [recording](examples/recording-record.json).

`Checkpoint` (required fields): runId (UUID v4), cursor (strict integer >=0),
simulationTime (UTC timestamp), stateRef (nonempty string, at most 200 characters),
stateSha256 (64 lowercase hexadecimal characters). Example:
[checkpoint](examples/checkpoint.json). These are internal values, not C01 endpoints.

The shared envelope is defined by `backend/app/shared/contracts.py`; producer
event payloads are referenced in events.yaml, never copied into the HTTP catalog.
TimeRange/Indicator are reused from C11's Python module. C12's namespaced OpenAPI
value schemas are exports of those same models for the aggregate catalog, not new
metric formulas or producer event definitions.

## Recording, reproducibility and checkpoint semantics

The manifest and [checkpoint](examples/checkpoint.json) reference immutable objects.
Fixture refs/hashes are placeholders, not verified real objects. Retain versioned
scenario/configuration, generator version and deterministic inputs; seed alone
does not establish reproducibility. Runtime must verify object integrity.

Cursor is the last applied sequence, zero for initial state. A checkpoint is the
state **after** its cursor. Apply full source outcomes by sequence even at equal
timestamps. Coalesced `CityStateUpdated` notifications cannot replace a complete
source recording. Validate sequence gaps, duplicate conflicts and checkpoint/range
consistency at runtime; individual models only validate shape/run scope.

Replay loads a checkpoint at/before the selected start and all essential outcomes
through the selected range. Missing configuration, checkpoint or essential event
makes that range unreconstructable; return CannotReplay and availableRanges.
After a read interruption retain the last successful state/cursor; resume retries
the next unread event exactly once.

## Timeline transport

The C01 play/pause/resume/seek/stop state table and HTTP error mapping now belong
to `c12ControlReplay` in [openapi.yaml](openapi.yaml). Scheduler natural completion
and read interruption still use the same recorded-state/cursor guarantees.
Speed is presentation pacing only. Its >0 and <=16 bound needs C01 review;
seek does not rerun today's planner. Stop/completion returns the existing frame;
additional final-summary fields need a future agreed DTO.

## Required interfaces and isolation

| Provider | Required contract/use | Status |
| --- | --- | --- |
| C02 | Authenticated roles/provider/run scope; recheck session ownership on every command. | Owner definition pending; payload identity is not authorisation. |
| C04/C05/C06/C08/C10 | Durable run-scoped source outcomes, plans/decisions and audit/record data as appropriate. | Producer definitions and exact subscriptions must be agreed, not copied into C12. C10 access may be internal rather than an event. |
| C06 | Original observation recording and historical state as needed. | [C06 internal.md](../c06-city-monitoring/internal.md) owns getCitySnapshot; notifications alone are not a full recording. |
| C10 | Authorised historical decision/evidence retrieval for runId + decisionId. | C10 owns selector and missing-evidence outcome; existing C12 DecisionSelector is only a consumer-side proposal pending #13/#34. |
| C11 | Recorded Indicator values in saved outcomes. | Reuse the value model; replay must not call current queryKPIs to replace history. |
| C01/infrastructure | View routes, pacing/page bounds, run storage, checkpoints, retention and isolated credentials. | Agree before implementation. |

Use recorded decisions, original KPI values and C10's original saved evidence.
Do not query today's C09 corpus for replacement evidence; show missing evidence
explicitly. `DecisionSelector(runId, decisionId)` in C12's typed models does not
redefine an authoritative C10 API. Method names/signatures belong to C10.

Simulation generation may use isolated simulated adapters. Playback cannot create
operational activity: read-only recorded-data credentials, separate writable
session storage, and no credentials/network route for live plan/resource writes,
vehicle commands, agent negotiation or broker publishing. [events.yaml](events.yaml)
describes the recorder's receive boundary, not a replay publication capability.
Integration tests with real restricted replay credentials must prove denied live
writes/publication before calling replay integrated.

## Persistence, verification and follow-up

No database changes. Follow `database/README.md` and shared Compose/.env.example.
Proposed schema `c12_simulation_replay`, manifest/event/checkpoint/session tables,
retention, keys and access roles need agreement before DDL. Write only owned state
and consume other components through their interfaces. Use UTC-stamped dbmate
migrations in `database/migrations/` with up/down, verify up → rollback → up, and
never rewrite merged migrations or create parallel Supabase migration history.
Outside-pair review is required; Bruno applies merged develop migrations to shared
Supabase. No manual shared-table edits or committed secrets. Schema ownership alone
does not prove replay isolation; use restricted recorded-data/session roles.

All examples are synthetic. Component models/tests check shape, run isolation,
finite pacing, UTC/ranges, seek and cursor boundaries, not persistent sequencing,
authorisation, recovery or broker ACLs. Run repository-root
`npm run contracts:lint`, `npm run contracts:build`, then backend `uv run pytest`,
`uv run ruff check .` and `uv run ruff format --check .`.
Component-specific invariants belong in backend/component tests, including recorded
Indicator values and replay interruption states. Runtime ownership, ordering and
transition checks remain required.
OpenAPI checks do not validate events/internal Markdown. Follow-up runtime work is
#17; simulation creation/full recording still needs its own agreed increment.
Prepared with AI assistance for Jolin; no handover, producer agreement, deployment
or completed paired work is claimed.
