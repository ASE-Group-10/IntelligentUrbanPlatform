# C12 Simulation and Replay: internal interfaces (UC12)

Status: proposal for producer/consumer review; relocation under issue #35
of the reviewed #15 / PR #22 increment. Runtime functionality is not delivered.
Pair: Jolin + Qingyuan; no completed paired work or UC12 handover is claimed.
Outside reviewers: Bruno (C06/C12) and Oscar (C08).

Follow [the component contract rules](../../README.md). HTTP routes and transport
envelopes were not agreed in #22, so this migration does not invent an
`openapi.yaml`. C01-facing HTTP adapters need a separately agreed contract;
the logical service calls below are documented as internal interfaces meanwhile.
RabbitMQ consumer proposals are in [events.yaml](events.yaml). No live worker,
route, database migration or algorithm is introduced.

Reuse `backend/app/shared/contracts.py` (`ContractModel`, `UtcDatetime`,
`EventEnvelope`) and `backend/app/shared/result.py` (`Ok<T> | Failure`).
JSON fields are camelCase, UUID entity IDs are v4, times are UTC with `Z`, unknown
fields are rejected, and ranges are half-open `[start, end)`. `schemaVersion`
and entity versions are distinct. An authenticated C02 context is passed
separately, never trusted from the payload. Models validate shape only:
server-side authorisation, transactional deduplication and ordering remain runtime work.

## Provided interfaces

### startSimulation(context, request: SimulationRequest) → Result<RunManifest>

Caller: C01 simulation adapter/authorised operator. Example:
[request](examples/start-simulation-request.json), [manifest](examples/run-manifest.json).
Persist an isolated run manifest, immutable configuration and initial state before
generation. Same provider/key/body returns the original run; changed body conflicts.
No live adapter writes are permitted. Failures: `Invalid` (scenario/configuration),
`Forbidden`, `Conflict` (key reuse), `Unavailable` (storage/generator dependency).
The manifest shape is a proposal; runtime and generator are not implemented.

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

### listRuns(context, filters) → Result<run page>

Caller: C01 adapter/authorised operator. Filters: provider UUID v4 (required and
checked against context), optional scenarioId string, recording status
(`recording`/`completed`/`incomplete`), period (`TimeRange`) and page cursor string.
Response contains `RunManifest` items including availableRanges and a bounded
continuation cursor. **Filter/page DTO, bounds and exact fields are still pending**;
the manifest is the currently typed part. Do not leak another provider's runs.
Failures: `Invalid`, `Forbidden`, `Unavailable`. No state mutation.

### startReplay(context, request: ReplayRequest) → Result<ReplayFrame or CannotReplay>

Caller: C01 replay adapter/authorised operator. Examples:
[request](examples/start-replay-request.json), [paused frame](examples/replay-frame-paused.json),
[cannot replay](examples/cannot-replay.json).
Verify selected run/range, configuration/checkpoint integrity and every essential
record before creating a private ready/paused session and its initial frame.
Missing essential data gives `Ok<CannotReplay>` with reason and availableRanges,
not false success. Failures: `Invalid`, `Forbidden`, `NotFound` (run),
`Unavailable` (store inaccessible). Session ownership must be checked separately.

### controlReplay(context, control: ReplayControl) → Result<ReplayFrame>

Caller: C01 replay adapter/session owner. Example: [seek](examples/control-replay-seek.json).
Validate ownership, expectedCursor, command transition, selected range and target
before mutation. Return actual frame/status. Failures: `Invalid` (command/target),
`Forbidden`, `NotFound` (session), `Conflict` (stale cursor/state), `Unavailable`.
For reads interrupted in an active session return a paused frame with the last
committed state/cursor and interruption reason, without skipping the next event.

### advanceReplay(sessionId, expectedCursor) → Result<ReplayFrame>

Caller: trusted replay scheduler. `sessionId`: UUID v4, required; `expectedCursor`:
nonnegative strict integer, required. Applies the next recorded outcome exactly
once; completed at range end. Failures: `Invalid`, `Forbidden`, `NotFound`,
`Conflict`, `Unavailable`. Interrupted recording reads pause without cursor advance.
This is a provided scheduler boundary; checkpoint loading/reconstruction helpers
are component implementation details and are not additional public contracts.

## Value-model fields

Typed models: `backend/app/components/c12_simulation_replay/contracts.py`.
TimeRange and Indicator are reused from C11, whose definitions are in
[C11 internal.md](../c11-kpi-analytics/internal.md); they are not duplicated.
Required means required JSON property, separately from nullable. Identifier strings
are nonempty and at most 200 characters. Sequence/cursor/seed use strict integers,
not booleans. The manifest's scope defaults to simulation and can only be simulation.

### SimulationRequest

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scenarioId` | string | yes | Versioned synthetic scenario identifier. |
| `seed` | integer | yes | Nonnegative deterministic input; seed alone does not guarantee reproducibility. |
| `configurationVersion` | string | yes | Immutable configuration reference/version. |
| `idempotencyKey` | string | yes | Provider-scoped mutation key; repeated body reuses the run, changed body conflicts. |

### RunManifest

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schemaVersion` | constant `1` | no | Contract shape version, separate from entity and metric definition versions. |
| `runId` | string (uuid4) | yes | Simulation run identity; must match every recorded event/session. |
| `providerId` | string (uuid4) | yes | Requested owner; checked against C02 context, never an access grant. |
| `scope` | constant `simulation` | no | Defaults to simulation; a manifest never describes a live run. |
| `scenarioId` | string | yes | Versioned synthetic scenario identifier. |
| `seed` | integer | yes | Nonnegative deterministic input; seed alone does not guarantee reproducibility. |
| `configurationVersion` | string | yes | Immutable configuration reference/version. |
| `status` | `recording`, `completed`, `incomplete` | yes | Domain state; cross-field/state-transition constraints are documented below. |
| `initialStateRef` | string | yes | Immutable initial-state object reference; example is a placeholder. |
| `initialStateSha256` | string | yes | 64 lowercase hex digest; runtime must verify the referenced object. |
| `availableRanges` | array of TimeRange | yes | Reconstructable recording intervals, not a promise that the entire run exists. |
| `lastSequence` | integer | yes | Last recorded sequence, zero before any event. |

### RecordingRecord

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `runId` | string (uuid4) | yes | Simulation run identity; must match every recorded event/session. |
| `sequence` | integer | yes | One-based contiguous run-local recording sequence. |
| `simulationTime` | string (date-time) | yes | Recorded simulation clock; apply equal-time outcomes in sequence order. |
| `event` | EventEnvelope | yes | Shared EventEnvelope with the same simulation run; payload owned by its producer. |

### Checkpoint

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `runId` | string (uuid4) | yes | Simulation run identity; must match every recorded event/session. |
| `cursor` | integer | yes | Last applied sequence; zero means initial state; checkpoint state is after this cursor. |
| `simulationTime` | string (date-time) | yes | Recorded simulation clock; apply equal-time outcomes in sequence order. |
| `stateRef` | string | yes | Immutable checkpoint object reference. |
| `stateSha256` | string | yes | 64 lowercase hex digest; runtime must verify object integrity. |

### ReplayRequest

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `runId` | string (uuid4) | yes | Simulation run identity; must match every recorded event/session. |
| `timeRange` | TimeRange | yes | Selected reconstructable half-open range; see C11 TimeRange. |
| `speed` | number | no | Finite presentation pacing > 0 and <= 16; proposed bound needs C01 agreement. |

### ReplayControl

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `sessionId` | string (uuid4) | yes | Private replay session, ownership checked on every command. |
| `command` | `play`, `pause`, `resume`, `seek`, `stop` | yes | Replay control; allowed transitions are documented below. |
| `expectedCursor` | integer | yes | Optimistic cursor check; stale cursor conflicts without state change. |
| `targetCursor` | integer or null | no | Required only for seek; runtime checks selected range and reconstructability. |

### ReplayFrame

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schemaVersion` | constant `1` | no | Contract shape version, separate from entity and metric definition versions. |
| `sessionId` | string (uuid4) | yes | Private replay session, ownership checked on every command. |
| `runId` | string (uuid4) | yes | Simulation run identity; must match every recorded event/session. |
| `cursor` | integer | yes | Last applied sequence; zero means initial state; checkpoint state is after this cursor. |
| `status` | `ready`, `playing`, `paused`, `completed`, `stopped` | yes | Domain state; cross-field/state-transition constraints are documented below. |
| `simulationTime` | string (date-time) | yes | Recorded simulation clock; apply equal-time outcomes in sequence order. |
| `state` | object | yes | Recorded/reconstructed state, never a trigger for live operations. |
| `decisionIds` | array of string (uuid4) | yes | Recorded decision references; use C10 historical evidence. |
| `recordedKpis` | array of Indicator | yes | Recorded C11 Indicator values; do not recalculate using today's configuration. |
| `interruption` | string or null | no | Read-failure reason; when present frame status must be paused. |

### CannotReplay

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `runId` | string (uuid4) | yes | Simulation run identity; must match every recorded event/session. |
| `reason` | string | yes | Explicit missing-data or CannotReplay reason. |
| `availableRanges` | array of TimeRange | yes | Reconstructable recording intervals, not a promise that the entire run exists. |

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

## Timeline semantics (proposed)

| Command/state | Behaviour |
| --- | --- |
| play from ready | playing |
| pause from playing | paused, retain state/cursor |
| resume from paused | playing, retry next unread event |
| seek from ready/playing/paused | Reconstruct to target within selected range, then paused; only seek accepts targetCursor. |
| stop from active session | stopped, retain final run/range/results metadata |
| natural range end | completed, retain final metadata |
| stale cursor/invalid transition | Conflict/Invalid, no cursor change |
| source interruption | paused, original state/cursor and explicit interruption |

Speed controls presentation only; the finite >0 and <=16 bound needs C01 agreement.
Seek never runs today's planner. Final metadata DTO is still pending; no additional
response fields are invented in this location-only change.

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
OpenAPI checks do not validate events/internal Markdown. Follow-up runtime work is
#17; simulation creation/full recording still needs its own agreed increment.
Prepared with AI assistance for Jolin; no handover, producer agreement, deployment
or completed paired work is claimed.
