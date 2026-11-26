# C11 KPI Analytics: internal interfaces (UC11)

Current integration increment (#16/#17): HTTP routing, C02 analyst/provider checks,
owned PostgreSQL storage and C04 receive workers are implemented. See the
[executable integration guide](../../../docs/c11-c12-integration.md) for commands,
recording coverage and outstanding agreements. This supersedes the original
proposal's no-runtime/no-database statements below. No completed paired session or
full thin-slice integration is claimed. C11 values remain unavailable until metric
formulas are agreed; C12 C04-only captures remain explicitly incomplete.

Current ownership integration: C11 atomically binds C04's authoritative
`payload.providerId` and rejects reassignment. Legacy events lacking that field
remain unbound/private. Root local Compose starts both workers and provisions a
restricted local playback login outside shared database migrations.


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
| queryKPIs | POST `/api/v1/analytics/kpis/query` |
| getKPIBreakdown | POST `/api/v1/analytics/kpis/{metricId}/breakdown` |

Schemas and request/response/error definitions belong only to openapi.yaml.
Existing value models remain in `backend/app/components/c11_kpi_analytics/contracts.py`.

## Provided internal interfaces

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
Component-specific invariants belong in backend/component tests. The service must
enforce records.length <= the requested limit, numeric coverage comparisons,
authorisation and state transitions.
OpenAPI checks do not validate events/internal Markdown. Future behaviour is #16;
agreement/review comes before runtime work. Prepared with AI assistance for Jolin;
no producer agreement, deployment or completed paired implementation is claimed.
