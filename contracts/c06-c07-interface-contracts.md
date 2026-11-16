# C06/C07 Interface Contracts (Draft)

13 Nov 2026 · Bruno Torrijo

These are draft contracts for every interface that C06 City Monitoring and C07 Disruption Detection share with another pair. They're here to be argued with at the contract workshop. The scope is the thin slice (16–27 Nov): one simulated road closure, carried from ingestion to a single replan request.

## Shared conventions

All pairs need to agree these once, and every contract below uses them.

| Item | Proposal |
| --- | --- |
| Identifiers | UUID v4 strings for every entity and event ID (`eventId`, `planId`, `disruptionId`, ...) |
| Versions | Integers starting at 1, incremented on every committed change to an entity |
| Timestamps | ISO 8601 in UTC with `Z` suffix, e.g. `2026-11-20T08:15:00Z`; UI converts to local time |
| Road segment key | `seg:<osm_way_id>` (OpenStreetMap way ID), e.g. `seg:4471023` |
| Zone key | `zone:<code>`, using a fixed grid or Dublin postal district (to decide) |
| Units | Speed in km/h, delay in seconds, rainfall in mm/h, temperature in °C, distance in metres |
| Quality | Enum `good` / `degraded` / `unknown` on any observed or derived value |
| Run scope | `live` or `simulation`, plus `runId` when simulation; replay never writes to `live` |
| Failure outcomes | The shared `Result<T>` kinds: `Invalid`, `Forbidden`, `NotFound`, `Conflict`, `Unavailable`, `Unconfirmed`, `Infeasible`, `Partial` |

Where a field is marked *slice: no*, it can be left out until Iteration 3 but the name is reserved.

## 1. EventEnvelope (all pairs)

Every message on RabbitMQ is one JSON EventEnvelope with a typed `payload`. Consumers deduplicate on `eventId`, and producers write the envelope to their outbox in the same transaction as the state change.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `eventId` | UUID | yes | Unique per event; retries reuse it |
| `eventType` | string | yes | `CityStateUpdated`, `ResourceAvailabilityChanged`, `OperationalDisruption`, `PlanReady`, ... |
| `schemaVersion` | integer | yes | Starts at 1; bump on breaking payload change |
| `producer` | string | yes | Component code, e.g. `C06` |
| `observedAt` | timestamp | yes | When the underlying thing happened in the world |
| `emittedAt` | timestamp | yes | When the producer committed the event |
| `correlationId` | UUID | yes | Ties a whole chain together (observation → disruption → replan) |
| `causationId` | UUID | no | `eventId` of the event that caused this one |
| `affected` | object | yes | `{ "planIds": [], "resourceIds": [], "segmentKeys": [] }`; empty arrays allowed |
| `entityVersions` | object | no | Map of entity ID → version this event refers to |
| `scope` | string | yes | `live` or `simulation` |
| `runId` | UUID | when simulation | C12 run that produced it |
| `payload` | object | yes | Type-specific body, defined in the sections below |

**Routing proposal:** one topic exchange `ulop.events`, routing key = `eventType`. C07 binds a queue to `CityStateUpdated`, `ResourceAvailabilityChanged` and `OperationalDisruption`.

```json
{
  "eventId": "5b1e0c1a-8f0e-4a7e-9d6c-1f2a3b4c5d6e",
  "eventType": "CityStateUpdated",
  "schemaVersion": 1,
  "producer": "C06",
  "observedAt": "2026-11-20T08:14:30Z",
  "emittedAt": "2026-11-20T08:14:31Z",
  "correlationId": "a7d9e2f4-3c1b-4b8a-9e6f-0d1c2b3a4f5e",
  "causationId": null,
  "affected": { "planIds": [], "resourceIds": [], "segmentKeys": ["seg:4471023"] },
  "entityVersions": { "citySnapshot": 42 },
  "scope": "simulation",
  "runId": "c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b",
  "payload": { }
}
```

## 2. City snapshot and CityStateUpdated (with Ye + Zecheng)

C06 owns City State; C05 and C07 read it through `getCitySnapshot`, and C07 is told about changes through `CityStateUpdated`. The event carries only what changed, and consumers call `getCitySnapshot` when they need full context.

### SegmentCondition (shared building block)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `segmentKey` | string | yes | `seg:<osm_way_id>` |
| `status` | enum | yes | `open` / `slow` / `closed` |
| `speedKmh` | number | no | Current average speed; null when closed or unknown |
| `delaySeconds` | integer | no | Extra travel time vs free flow |
| `cause` | enum | no | `traffic` / `roadworks` / `event` / `weather` / `incident` / `manual` |
| `validUntil` | timestamp | no | Expected end, e.g. a manual report's expiry |
| `observedAt` | timestamp | yes | Time of the latest observation used |
| `quality` | enum | yes | `good` / `degraded` / `unknown` |
| `sources` | array of string | yes | Source IDs that contributed, e.g. `["sim-traffic"]` |
| `conflict` | boolean | no | *slice: no*. True when sources disagree (AF3) |

Zone-level weather (`zoneKey`, `rainMmH`, `tempC`, `windKmh`, `quality`) follows the same pattern. *slice: no*.

### getCitySnapshot(scope, freshnessPolicy) → Result<CitySnapshot>

**Request**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scope.segmentKeys` | array of string | one of | Specific segments |
| `scope.bbox` | [minLon, minLat, maxLon, maxLat] | one of | Area query |
| `freshnessPolicy.maxAgeSeconds` | integer | no | Values older than this come back `degraded`; default 900 |
| `atVersion` | integer | no | Read a past snapshot version (used by C10/C12) |

**Response: CitySnapshot**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `snapshotVersion` | integer | yes | Monotonic City State version |
| `generatedAt` | timestamp | yes | When this snapshot was assembled |
| `segments` | array of SegmentCondition | yes | Only segments with a non-default condition; absent = `open`, `good` |
| `staleSources` | array of string | yes | Empty when all feeds are fresh |
| `overallQuality` | enum | yes | Worst quality among returned values |

**Failures:** `Invalid` (bad scope), `Unavailable` (no City State yet, or store down), `Forbidden`.

### CityStateUpdated payload

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `snapshotVersion` | integer | yes | Version after this change |
| `previousVersion` | integer | yes | Version it replaced |
| `changes` | array of SegmentCondition | yes | Latest state of each changed key only |
| `degraded` | boolean | yes | True when published by the freshness check (AF2) |
| `staleSources` | array of string | yes | Empty unless `degraded` |

```json
{
  "snapshotVersion": 42,
  "previousVersion": 41,
  "changes": [
    {
      "segmentKey": "seg:4471023",
      "status": "closed",
      "speedKmh": null,
      "delaySeconds": null,
      "cause": "roadworks",
      "validUntil": "2026-11-20T18:00:00Z",
      "observedAt": "2026-11-20T08:14:30Z",
      "quality": "good",
      "sources": ["sim-traffic"]
    }
  ],
  "degraded": false,
  "staleSources": []
}
```

## 3. findAffectedWork and requestReplan (with Ye + Zecheng)

C07 asks C05 which unfinished work a change touches, then sends one `requestReplan` per affected plan. Both are synchronous internal calls. C05 owns plans, so it decides what "affected" means for a route; C07 only says where and what changed.

### findAffectedWork(changeScope) → Result<AffectedWork>

**Request: changeScope**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `segmentKeys` | array of string | one of | Segments whose condition changed |
| `resourceIds` | array of UUID | one of | Resources whose availability changed or failed |
| `planIds` | array of UUID | one of | Plans named directly (e.g. by `OperationalDisruption`) |
| `from` / `to` | timestamp | no | Time window the change applies to; default now → open-ended |

**Response: AffectedWork**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `items` | array | yes | Empty when nothing is affected |
| `items[].planId` | UUID | yes | Plan touching the scope |
| `items[].planVersion` | integer | yes | Current version C05 checked against |
| `items[].deliveryIds` | array of UUID | yes | Unfinished deliveries on the affected part; completed ones excluded |
| `items[].matchedSegmentKeys` | array of string | no | Which of the requested segments the route uses |
| `items[].matchedResourceIds` | array of UUID | no | Which requested resources the plan relies on |

**Failures:** `Invalid`, `Unavailable` (C07 then reports disruption status `unknown`).

### requestReplan(context, planId, trigger, key) → Result<ReplanOutcome>

**Request**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `context` | AuthContext | yes | Service identity `C07`, plus `correlationId` |
| `planId` | UUID | yes | Plan to revise |
| `trigger.disruptionId` | UUID | yes | Disruption record behind the request |
| `trigger.disruptionVersion` | integer | yes | Version of that record |
| `trigger.basedOnPlanVersion` | integer | yes | Plan version returned by `findAffectedWork` |
| `trigger.reason` | enum | yes | `road_closed` / `congestion` / `resource_unavailable` / `operational_failure` / `weather` |
| `trigger.citySnapshotVersion` | integer | no | Snapshot the disruption was judged on |
| `trigger.sourceEventId` | UUID | yes | Event that started the chain |
| `key` | string | yes | Idempotency key = `planId:disruptionId:disruptionVersion` |

**Response: ReplanOutcome**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `status` | enum | yes | `NoChange` / `ReviewRequired` / `PlanSaved` / `PublicationPending` |
| `newPlanVersion` | integer | when saved | Version of the saved revision |
| `decisionId` | UUID | when saved | C10 decision record |

**Failures:** `Conflict` (plan version moved on; C07 re-runs `findAffectedWork` once), `Infeasible`, `Unavailable`, `NotFound`.

The idempotency key is what stops the C08 → C07 → C05 loop. The same disruption version can never produce a second replan of the same plan; C05 returns the first outcome again.

```json
{
  "planId": "9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c",
  "trigger": {
    "disruptionId": "d4c3b2a1-0f9e-4d8c-b7a6-5e4d3c2b1a0f",
    "disruptionVersion": 1,
    "basedOnPlanVersion": 3,
    "reason": "road_closed",
    "citySnapshotVersion": 42,
    "sourceEventId": "5b1e0c1a-8f0e-4a7e-9d6c-1f2a3b4c5d6e"
  },
  "key": "9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c:d4c3b2a1-0f9e-4d8c-b7a6-5e4d3c2b1a0f:1"
}
```

## 4. ResourceAvailabilityChanged (with Julian + Jolin)

C03 publishes this from its outbox whenever `updateResourceStatus` commits a change in observed availability. C07 only needs to know which resource, what changed and how reliable the information is. When the event is incomplete it falls back to C03 `getResource`. Committed capacity stays with C08 and is not in this event.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `resourceId` | UUID | yes | Vehicle, depot, warehouse or locker |
| `resourceType` | enum | yes | `vehicle` / `depot` / `warehouse` / `locker` |
| `resourceVersion` | integer | yes | C03 version after the change |
| `previousStatus` | enum | yes | `available` / `limited` / `unavailable` / `unknown` |
| `newStatus` | enum | yes | Same values |
| `reason` | string | no | Free text, e.g. `breakdown`, `depot closed` |
| `location` | {lat, lon} | no | Last known position (vehicles) |
| `capacityChange` | object | no | *slice: no*. e.g. `{ "lockerSlots": -12 }` for partial loss |
| `expectedBackAt` | timestamp | no | When the resource should be available again |
| `quality` | enum | yes | `good` / `degraded` / `unknown` |

**For the slice:** C07 can ignore this event, since the demo path is a road closure. Agree the shape now so the queue binding and fixtures exist.

```json
{
  "resourceId": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
  "resourceType": "vehicle",
  "resourceVersion": 7,
  "previousStatus": "available",
  "newStatus": "unavailable",
  "reason": "breakdown",
  "location": { "lat": 53.3438, "lon": -6.2546 },
  "expectedBackAt": null,
  "quality": "good"
}
```

## 5. OperationalDisruption (with Shixian + Oscar)

C08 publishes this when an allocation commit fails or a resource becomes invalid during execution (UC04 lost availability, UC08 commit failure). It reports the actual committed state after the failure, so C07 never has to guess what is still allocated. C08 does not wait for C07.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `failureId` | UUID | yes | C08's stored failed-outcome record |
| `failureKind` | enum | yes | `commit_conflict` / `capacity_exceeded` / `resource_lost` / `driver_unavailable` / `agent_timeout` |
| `planId` | UUID | yes | Plan whose execution failed |
| `planVersion` | integer | yes | Version C08 was executing |
| `deliveryIds` | array of UUID | yes | Deliveries left without a valid commitment |
| `resourceIds` | array of UUID | yes | Resources involved in the failure |
| `committedState` | object | yes | `{ "stillCommitted": [allocationId], "released": [allocationId] }` |
| `decisionId` | UUID | no | C10 record of the failed attempt |
| `detail` | string | no | Human-readable reason for logs and the UI |

C07 treats `failureId` as the trigger identity. A redelivered or repeated event with the same `failureId` creates no new disruption version and no new replan.

```json
{
  "failureId": "f0e1d2c3-b4a5-4968-8776-655443322110",
  "failureKind": "resource_lost",
  "planId": "9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c",
  "planVersion": 3,
  "deliveryIds": ["7c6b5a49-3827-4165-a0f9-e8d7c6b5a493"],
  "resourceIds": ["1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"],
  "committedState": { "stillCommitted": [], "released": ["2b3c4d5e-6f70-4819-9a0b-1c2d3e4f5a6b"] },
  "decisionId": null,
  "detail": "Vehicle reported breakdown before pickup"
}
```

## 6. Models we own internally

These are for the other pairs to review, not agree field by field. C10 and C12 will read them, so they should be stable.

**Observation** (C06, input to `ingestObservation`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `observationId` | UUID | yes | Assigned by source or C06; dedup key |
| `sourceId` | string | yes | e.g. `sim-traffic`, `manual` |
| `kind` | enum | yes | `traffic` / `weather` / `event` / `closure` |
| `target` | object | yes | `{ "segmentKey" }` or `{ "zoneKey" }` or `{ "lat", "lon" }` (C06 maps to a segment) |
| `values` | object | yes | Raw values in source units, e.g. `{ "status": "closed" }` |
| `observedAt` | timestamp | yes | Source time; older than current → history only (AF4) |
| `author` / `expiresAt` | string / timestamp | manual only | AF5 |

Ingestion returns `IngestionReceipt { observationId, disposition, snapshotVersion }`, with `disposition` = `merged` / `history_only` / `conflict` / `rejected`.

**Disruption** (C07, persisted by `upsertDisruption`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `disruptionId` | UUID | yes | Stable across updates |
| `version` | integer | yes | Bumped only when impact changes materially |
| `status` | enum | yes | `active` / `resolved` / `unknown` |
| `cause` | enum | yes | Same as SegmentCondition `cause`, plus `resource` / `operational` |
| `severity` | enum | yes | `low` / `medium` / `high` |
| `segmentKeys` / `resourceIds` | arrays | yes | What is disrupted |
| `affectedPlans` | array | yes | `[{ planId, planVersion, replanRequested, replanOutcome }]` |
| `triggerEventIds` | array of UUID | yes | Every event folded into this record (dedup) |
| `quality` | enum | yes | Confidence in the classification |
| `createdAt` / `updatedAt` | timestamp | yes | |

## Open questions for the workshop

- [ ] Segment keys: are OSM way IDs fine for C05's routing graph, or does C05 use its own edge IDs?
- [ ] Zones: fixed grid or postal districts? Only needed for weather, so this can wait.
- [ ] Default `maxAgeSeconds` for freshness (900 s proposed) and per-source overrides.
- [ ] What counts as a "material" change that bumps a disruption version (proposed: status or severity changes, or new affected plans).
- [ ] Severity rules for the slice (proposed: `closed` on a used segment = `high`; anything else is out of scope until Iteration 3).
- [ ] Is `findAffectedWork` one C05 call per change, or should C07 batch several segments together?
- [ ] Does C05 accept `requestReplan` synchronously, or return `PublicationPending` and finish in a worker?
- [ ] RabbitMQ exchange and queue naming, and who creates them (proposed: a shared setup script in the repo).
- [ ] If we take over UC12: does C12 replay read events from a C06/C07 history table or from a separate run log?
