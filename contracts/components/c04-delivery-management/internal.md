# C04 Delivery Management: internal interfaces

C04 validates and owns delivery requests, their constraints, and authoritative
progress. A stored request is not a feasible plan, an allocation, or a dispatch
instruction. HTTP operations are defined in [openapi.yaml](openapi.yaml), and
outbound messages are defined in [events.yaml](events.yaml).

## Conventions

Use camelCase JSON, UUID v4 identifiers, positive integer versions, and UTC
RFC 3339 timestamps. Mutating operations use an idempotency key and expected
versions. State changes and their outbox entries commit atomically. Consumers
deduplicate by event ID and reject stale delivery versions.

## Shared parameters: `AuthContext` and `RunScopeSelector`

- **`AuthContext`** is owned by C02 (see
  [C02 internal.md](../c02-identity-access/internal.md)). A caller-supplied
  provider ID, delivery ID, or `runId` is a selector, never permission; C04
  checks every call against `context`.
- **`RunScopeSelector`** follows the shared `RunScope` contract in
  `backend/app/shared/contracts.py` (`EventMetadata`) and
  [C12 internal.md](../c12-simulation-replay/internal.md):

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scope` | enum (`live`, `simulation`) | yes | Distinguishes live operations from an isolated simulation run |
| `runId` | UUID or null | conditional | Required UUID v4 when `scope` is `simulation`; must be `null`/absent when `scope` is `live` |

### Run-scope isolation rules

1. **Strict validation:** Passing `scope: "live"` with a non-null `runId`, or
   passing `scope: "simulation"` without a valid UUID v4 `runId`, fails
   immediately with `Invalid` (`C04.INVALID_RUN_SCOPE`). An unspecified
   simulation run is never silently treated as `live`.
2. **Authorisation:** C04 verifies with C02 that `context` is authorised for
   the requested `runScope` (including the specific `runId` for simulation
   calls). Unauthorised callers receive `Forbidden` without revealing whether
   records exist.
3. **Partitioned storage, idempotency, and versions:** Every delivery request
   is permanently bound to the `(scope, runId)` in which it was created.
   Reads, mutations, expected-version checks, idempotency keys, and outbox
   events are strictly partitioned by `(scope, runId)`. Emitted events carry
   the same `scope` and `runId` in their `EventEnvelope`.
4. **Cross-run and cross-scope rejection:** If any `deliveryId` passed to
   `getRequests`, `markRequestsPlanned`, or `updateProgress` belongs to a
   different `runId` or to the opposite `scope` (`live` vs `simulation`), C04
   rejects the entire call with `NotFound` (`C04.DELIVERY_NOT_FOUND`) and
   commits **no changes** (no partial links, no progress updates, no outbox
   entries). Returning `NotFound` avoids leaking whether an entity exists in
   another run or in live state.
5. **Isolated simulation writers vs. C12 replay:** Isolated simulation
   services (for example C05 planning or simulated status adapters running
   inside a simulation run) may call `getRequests`, `markRequestsPlanned`, and
   `updateProgress` with `scope: "simulation"` and their authorised `runId`.
   **C12 replay playback is strictly read-only** over saved simulation
   outcomes and C10 saved evidence; replay must never call
   `markRequestsPlanned`, `updateProgress`, or any mutating C04 operation, and
   must never read or change live requests.

Examples: [simulation `getRequests`](examples/simulation-get-requests.json) and
[cross-run failure](examples/simulation-cross-run-failure.json).

## Provided interfaces

### getRequests(context, runScope, requestIds) → Result\<DeliveryRequest[]\>

C05 reads the authoritative request facts before generating or revising a plan
(including UC07 replanning). The response includes the current version and
progress, but never a selected vehicle, route, allocation, or execution
guarantee.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `context` | AuthContext | yes | Caller identity, roles, provider scope, and correlation ID |
| `runScope` | RunScopeSelector | yes | Target `scope` (`live` or `simulation`) and conditional `runId` |
| `requestIds` | array of UUID | yes | Delivery requests to read; must contain at least one ID |

Each returned `DeliveryRequest` contains the fields in the `C04Delivery`
schema, including `deliveryId`, `version`, `pickup`, `destination`, `cargo`,
`timeWindow`, `constraints` (`vehicleKinds`: `van`, `truck`, `cargo_bike`),
`status`, and `progress`.

**Failures:** `Invalid` (including `C04.INVALID_RUN_SCOPE`), `Forbidden`,
`NotFound` (`C04.DELIVERY_NOT_FOUND` when any requested ID does not exist in
the given `runScope`, including cross-run or live/simulation mismatches),
`Unavailable`.

### markRequestsPlanned(context, runScope, requestIds, planId, expectedVersions, key) → Result\<Receipt\>

C05 calls this operation in the same logical confirmation transaction as its
plan state and `PlanReady` outbox entry. It links requests to a saved plan
within the given `runScope`; it does not allocate resources or mark deliveries
as in progress.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `context` | AuthContext | yes | Authorised C05 service identity |
| `runScope` | RunScopeSelector | yes | Target `scope` (`live` or `simulation`) and conditional `runId` |
| `requestIds` | array of UUID | yes | Requests included in the confirmed plan |
| `planId` | UUID | yes | C05 plan that owns the link |
| `expectedVersions` | map UUID → integer | yes | Version checked for every request |
| `key` | string | yes | Idempotency key scoped to caller, operation, and `runScope` |

**Response: Receipt**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `requestIds` | array of UUID | yes | Requests linked by this receipt |
| `planId` | UUID | yes | Confirmed plan reference |
| `versions` | map UUID → integer | yes | Versions after the link |

**Failures:** `Invalid` (`C04.INVALID_RUN_SCOPE` or malformed fields),
`Forbidden`, `NotFound` (`C04.DELIVERY_NOT_FOUND` if any request is missing or
belongs to another `runScope`; no partial links are committed), `Conflict`,
`Unavailable`. `Conflict` means a request version or status changed; no partial
links are committed. Retrying the same scoped key and request returns the
first receipt.

### updateProgress(context, runScope, deliveryId, progress, key) → Result\<DeliveryRequest\>

The authorised provider/status adapter (or isolated simulation status adapter)
uses this operation to record actual progress within `runScope`. It emits
`DeliveryProgressChanged` with the same `scope` and `runId`; it does not change
planning or allocation state.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `context` | AuthContext | yes | Authorised provider or simulation status adapter identity |
| `runScope` | RunScopeSelector | yes | Target `scope` (`live` or `simulation`) and conditional `runId` |
| `deliveryId` | UUID | yes | Delivery whose progress changed |
| `progress` | C04Progress | yes | Authoritative progress status and business occurrence time (`updatedAt`) |
| `key` | string | yes | Idempotency key scoped to caller, operation, and `runScope` |

`progress.updatedAt` is the actual business occurrence time (UTC RFC 3339) when
the progress transition happened (e.g., actual delivery time when
`status == "delivered"`), not database commit time or event `emittedAt`.

**Failures:** `Invalid` (`C04.INVALID_RUN_SCOPE` or invalid transition),
`Forbidden`, `NotFound` (`C04.DELIVERY_NOT_FOUND` if missing or in another
`runScope`; no changes committed), `Conflict`, `Unavailable`.

## Required interfaces

| Provider | Operation | Use by C04 |
| --- | --- | --- |
| C02 | `authorize(context, action, scope)` | Authorise submit, read, update, and provider progress operations (see [C02 internal.md](../c02-identity-access/internal.md)) |
| Mapping adapter | `validateLocations(pickup, destination)` | Confirm both locations before storing a request |
| C10 | `recordOutcome(conn, ctx, outcome) → Result<AuditId>` | Record the result of protected commands and progress changes (see [C10 internal.md](../c10-decision-records/internal.md)) |

The provider owns the definitions of these required operations; C04 does not
copy their schemas here.

### C10 outcome recording

C04 invokes C10's `recordOutcome(conn, ctx, outcome) → Result<AuditId>`
in-process following [C10 internal.md](../c10-decision-records/internal.md):

- **Transactional behaviour:** For committed mutations, `recordOutcome` runs
  inside C04's database transaction on the caller's `conn` and never commits on
  its own (if C04 rolls back, the audit record rolls back too). Rejected
  operations (`Invalid`, `Forbidden`, `NotFound`, `Conflict`, `Unavailable`)
  are recorded in their own short transaction.
- **Context propagation:** `principalId` and `correlationId` are taken from
  `ctx` (`AuthContext`).
- **Operation naming:** `outcome.operation` uses the `C04.<operation>` format.
- **Recorded C04 operations:**
  - `C04.submitDeliveryRequest` (HTTP `POST /api/v1/deliveries`)
  - `C04.updateDeliveryRequest` (HTTP `POST /api/v1/deliveries/{deliveryId}`)
  - `C04.markRequestsPlanned` (internal plan confirmation link from C05)
  - `C04.updateProgress` (internal progress update from provider/simulation adapter)

## Ownership and ordering

- C04 owns request identity, locations, cargo, constraints, request status, and
  progress. C05 owns plan state; C08 owns allocation and execution commitments.
- `pending` requests may be edited. Once linked to a confirmed plan, request
  facts are immutable through the update operation.
- A retry with the same caller, `runScope`, operation, key, and body returns
  the original committed result. Reusing a key with a different body returns
  `Conflict`.
- Events retain their `eventId` on transport retry. Consumers use
  `entityVersions[deliveryId]` and `deliveryVersion` to ignore stale updates.
