# C11 KPI Analytics: internal interfaces (UC11)

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

### consumeOperationalEvent(event) → Result<projection receipt>

Callers: trusted operational-event adapters for C04/C05/C06/C08, with C10
outcome/record access to be agreed. Input is the producer-owned `EventEnvelope`:
`eventId`, `eventType`, `schemaVersion`, `producer`, `observedAt`, `emittedAt`,
`correlationId`, `affected`, `scope`, `payload`, plus optional `causationId`,
`entityVersions` and simulation `runId`. See [C06's shared envelope definition](../c06-city-monitoring/events.yaml).
Check producer identity, event type/version and authorised scope before processing.

Atomically deduplicate eventId with the read-model update, preserve source/coverage
provenance and return the same receipt on redelivery. Never send an operational
command. **Receipt fields and the producer subscription set remain unagreed**;
this format migration does not fabricate a DTO or declare subscriptions to events
whose owners have not published contracts. [events.yaml](events.yaml) references
the existing C06 definition as a proposed input, not an agreed metric formula.

Failures: `Invalid` (unsupported envelope/payload/version), `Forbidden`
(untrusted producer/scope), `Conflict` (incompatible duplicate), `Unavailable`
(read-model/receipt store; no partial commit). Transient delivery retry policy
remains an infrastructure agreement.

### queryKPIs(context, query: KPIQuery) → Result<KPISet>

Callers: the C01 adapter/authorised analyst; C12 reads recorded values rather than
recomputing them. Request and response fields are defined below.
Examples: [request](examples/query-kpis-request.json),
[partial response](examples/kpi-set-partial.json),
[stale response](examples/kpi-set-stale.json).

Enforce provider/area/resource permissions for every query. Never combine live and
simulation populations. Requested definition versions must exist: no silent
formula substitution. Query is read-only and creates no plans or allocations.
Failures: `Invalid` (bad range/scope/definition), `Forbidden`, `Unavailable`
(no answer and no previous result). Missing individual values are explicit domain
data, not a numeric zero. A usable but incomplete set is `Ok<KPISet>` with
`status: partial`, not a second invented Result envelope.

### getKPIBreakdown(context, metricId, query: KPIQuery) → Result<Breakdown>

Callers: C01 KPI drill-down adapter/authorised analyst. `metricId` is a required
nonempty string (at most 200 characters); query fields are the same as queryKPIs.
Example: [breakdown](examples/kpi-breakdown.json).
Return bounded supporting records/aggregates, source IDs and optional nextCursor;
record schema, page limits and cursor request DTO still need consumer agreement.
Keep the original definition version and permissions; never leak another provider.
Failures: `Invalid`, `Forbidden`, `NotFound` (metric), `Unavailable`.

## Value-model fields

Typed models: `backend/app/components/c11_kpi_analytics/contracts.py`.
Required means required in the JSON object; nullable is shown separately.
Identifier strings are nonempty and at most 200 characters; count fields are
nonnegative integers, not booleans. Numbers must be finite.

### TimeRange

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `start` | string (date-time) | yes | Inclusive UTC start; must precede end. |
| `end` | string (date-time) | yes | Exclusive UTC end. |

### AnalysisScope

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `period` | TimeRange | yes | Half-open observation period; see TimeRange. |
| `providerId` | string (uuid4) | yes | Requested owner; checked against C02 context, never an access grant. |
| `areaId` | string or null | no | Optional requested area; identifier semantics need producer agreement. |
| `resourceIds` | array of string (uuid4) | no | Requested resources; server enforces provider and resource scope. |
| `scope` | `live`, `simulation` | yes | Live/simulation selection; simulation requires runId, live forbids it. |
| `runId` | string (uuid4) or null | no | Simulation run identity; must match every recorded event/session. |

### Coverage

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `eligible` | integer | yes | Eligible population under metricDefinitionVersion. |
| `observed` | integer | yes | Observed eligible members; cannot exceed eligible. |
| `excluded` | integer | no | Separately excluded records, not unobserved members. |

### Indicator

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `metricId` | string | yes | Stable metric key; unique within KPISet. |
| `value` | number or null | yes | Finite numeric result, or null for unavailable data; missing is never zero. |
| `unit` | string | yes | Explicit physical/currency unit; formulas and source units need agreement. |
| `status` | `available`, `partial`, `unavailable` | yes | Domain state; cross-field/state-transition constraints are documented below. |
| `coverage` | Coverage | yes | Population counts; see Coverage. |
| `lastUpdated` | string (date-time) or null | yes | Original source/result time; usable values require it; refresh never rewrites it. |
| `estimated` | boolean | yes | Whether the value is an estimate rather than measured data. |
| `estimationBasis` | string or null | no | Versioned rates/factors/model reference; required for estimates. |
| `reason` | string or null | no | Explicit missing-data or CannotReplay reason. |

### TrendPoint

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `period` | TimeRange | yes | Half-open observation period; see TimeRange. |
| `indicator` | Indicator | yes | Period-specific value using the same metric unit and definition version. |

### KPIQuery

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scope` | AnalysisScope | yes | Live/simulation selection; simulation requires runId, live forbids it. |
| `metricDefinitionVersion` | string | yes | Requested formula/population configuration; no silent substitution. |

### KPISet

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scope` | AnalysisScope | yes | Live/simulation selection; simulation requires runId, live forbids it. |
| `metricDefinitionVersion` | string | yes | Requested formula/population configuration; no silent substitution. |
| `schemaVersion` | constant `1` | no | Contract shape version, separate from entity and metric definition versions. |
| `generatedAt` | string (date-time) | yes | Response creation time, not observation freshness. |
| `status` | `current`, `partial`, `stale` | yes | Domain state; cross-field/state-transition constraints are documented below. |
| `indicators` | array of Indicator | yes | Nonempty metric values; unavailable and partial values remain explicit. |
| `trends` | array of TrendPoint | no | Period-specific points; defaults to an empty list. |
| `failure` | string or null | no | Refresh failure reason, required only for stale KPI sets. |

### Breakdown

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scope` | AnalysisScope | yes | Live/simulation selection; simulation requires runId, live forbids it. |
| `metricDefinitionVersion` | string | yes | Requested formula/population configuration; no silent substitution. |
| `metricId` | string | yes | Stable metric key; unique within KPISet. |
| `records` | array of object | yes | Bounded supporting records/aggregates; detailed record schema remains pending. |
| `sourceEventIds` | array of string (uuid4) | yes | Source provenance; resolve only within the authorised scope. |
| `nextCursor` | string or null | no | Opaque continuation cursor; null/absent means no next page. |

## Result semantics and unresolved metric definitions

- Missing: null value, unavailable status and explicit reason, never zero.
- A measured zero is valid only with observations. Usable indicators need a
  value and lastUpdated; no observations cannot produce a value.
- observed cannot exceed eligible. `available` requires observed == eligible;
  incomplete coverage is partial. Excluded records are counted separately.
- Estimates require versioned estimationBasis and are never labelled measured.
- Trends use period-specific indicators with matching units/definition versions.
- A refresh failure retains previous values and original lastUpdated, returns
  `status: stale` and failure. Without previous results return Unavailable;
  generatedAt is response time. Current sets cannot contain partial/missing items.
- Initial families: delivery time, cost, congestion and environmental impact.
  Formulas, weights, rates, factors, denominators, freshness thresholds and exact
  source payloads are not approved. `fixture-v1-not-approved`, seconds/EUR and
  synthetic supporting records illustrate shape, not production calculation.

## Required interfaces and agreements

| Provider | Required contract/use | Status |
| --- | --- | --- |
| C02 | Authenticated principal, roles, provider/area/resource authorisation. | Owning pair defines it; no local replacement AuthContext. |
| C04/C05/C08 | Authoritative delivery, plan, movement and allocation outcomes used for populations and metrics. | Owners must publish payloads/units/versions; no copied definitions here. |
| C06 | City/observation data for congestion and affected-area semantics. | Refer to [C06 internal interfaces](../c06-city-monitoring/internal.md) and its event definition; metric input selection pending. |
| C10 | Operational outcome/record access and audit integration if needed. | Owning pair must define access and retention. |
| C01 / consumers | Metric configuration/version, breakdown paging and view adapter. | Agree before runtime implementation. |

Only the provider defines a required operation. No producer schema or internal
component helper is redefined here. This proposal changes no other component.

## Persistence, verification and follow-up

No database changes. Follow `database/README.md` and the shared Compose/.env.example.
Proposed schema `c11_kpi_analytics` needs database-owner agreement. Write only its
own read model/receipts and read operational data through owner interfaces.
Use UTC-stamped dbmate migrations under `database/migrations/` with working up/down;
verify local up → rollback → up, never edit merged migrations or create a parallel
Supabase history. Outside-pair review is required; Bruno applies merged develop
migrations to shared Supabase. Never manually change shared tables or commit secrets.
Agree retention, keys and access before DDL.

All examples are synthetic. Tests read the component-owned examples and validate
Pydantic shape, null/zero, coverage, timestamps and staleness; they do not prove
authorisation, persistence, deduplication or integration. Run repository-root
`npm run contracts:lint`, `npm run contracts:build`, then backend `uv run pytest`,
`uv run ruff check .` and `uv run ruff format --check .`.
OpenAPI checks do not validate events/internal Markdown. Future behaviour is #16;
agreement/review comes before runtime work. Prepared with AI assistance for Jolin;
no producer agreement, deployment or completed paired implementation is claimed.
