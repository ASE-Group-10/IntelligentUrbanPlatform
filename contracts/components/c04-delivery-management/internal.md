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

## Provided interfaces

### getRequests(context, requestIds) → Result\<DeliveryRequest[]\>

C05 reads the authoritative request facts before generating or revising a plan.
The response includes the current version and progress, but never a selected
vehicle, route, allocation, or execution guarantee.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `context` | AuthContext | yes | Caller identity, roles, scope, and correlation ID |
| `requestIds` | array of UUID | yes | Delivery requests to read; must contain at least one ID |

Each returned `DeliveryRequest` contains the fields in the
`C04Delivery` schema, including `deliveryId`, `version`, `pickup`,
`destination`, `cargo`, `timeWindow`, `constraints`, `status`, and `progress`.

**Failures:** `Invalid`, `Forbidden`, `NotFound`, `Unavailable`.

### markRequestsPlanned(context, requestIds, planId, expectedVersions, key) → Result\<Receipt\>

C05 calls this operation in the same logical confirmation transaction as its
plan state and `PlanReady` outbox entry. It links requests to a saved plan; it
does not allocate resources or mark deliveries as in progress.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `context` | AuthContext | yes | Authorised C05 service identity |
| `requestIds` | array of UUID | yes | Requests included in the confirmed plan |
| `planId` | UUID | yes | C05 plan that owns the link |
| `expectedVersions` | map UUID → integer | yes | Version checked for every request |
| `key` | string | yes | Idempotency key for the confirmation operation |

**Response: Receipt**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `requestIds` | array of UUID | yes | Requests linked by this receipt |
| `planId` | UUID | yes | Confirmed plan reference |
| `versions` | map UUID → integer | yes | Versions after the link |

**Failures:** `Invalid`, `Forbidden`, `NotFound`, `Conflict`, `Unavailable`.
`Conflict` means a request version or status changed; no partial links are
committed. Retrying the same scoped key and request returns the first receipt.

### updateProgress(context, deliveryId, progress, key) → Result\<DeliveryRequest\>

The authorised provider/status adapter uses this operation to record actual
progress. It emits `DeliveryProgressChanged`; it does not change planning or
allocation state.

**Failures:** `Invalid`, `Forbidden`, `NotFound`, `Conflict`, `Unavailable`.

## Required interfaces

| Provider | Operation | Use by C04 |
| --- | --- | --- |
| C02 | `authorize(context, action, scope)` | Authorise submit, read, update, and provider progress operations |
| Mapping adapter | `validateLocations(pickup, destination)` | Confirm both locations before storing a request |
| C10 | `recordOutcome(outcome)` | Record the result of protected commands and progress changes |

The provider owns the definitions of these required operations; C04 does not
copy their schemas here.

## Ownership and ordering

- C04 owns request identity, locations, cargo, constraints, request status, and
  progress. C05 owns plan state; C08 owns allocation and execution commitments.
- `pending` requests may be edited. Once linked to a confirmed plan, request
  facts are immutable through the update operation.
- A retry with the same caller, scope, operation, key, and body returns the
  original committed result. Reusing a key with a different body returns
  `Conflict`.
- Events retain their `eventId` on transport retry. Consumers use
  `entityVersions[deliveryId]` and `deliveryVersion` to ignore stale updates.
