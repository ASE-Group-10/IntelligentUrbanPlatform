# C07 Disruption Detection: internal interfaces

C07 evaluates disruptions (UC06) and requests separate replanning. It has no
HTTP operations. The events it consumes are in `events.yaml`, and its
`evaluateChange` handler is triggered by them. This file covers the calls C07
makes to other components and the record it owns.

Typed models: `backend/app/components/c07_disruption_detection/contracts.py`.
Conventions (IDs, versions, timestamps, quality, run scope, `Result` failures):
see `../c06-city-monitoring/internal.md`.

## Provided interfaces

None. Other components learn about disruptions through the replan requests C07
sends to C05.

### Owned record: Disruption

C10 and C12 will read it, so it should stay stable. Persisted by
`upsertDisruption`. Example: `examples/disruption.json`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `disruptionId` | UUID | yes | Stable across updates |
| `version` | integer | yes | Bumped only on a material change: status or severity changes, or a new affected plan |
| `status` | enum | yes | `active` / `resolved` / `unknown` |
| `cause` | enum | yes | Same as SegmentCondition `cause`, plus `resource` / `operational` |
| `severity` | enum | yes | `low` / `medium` / `high`. Slice rule: `closed` on a used segment = `high` |
| `segmentKeys` / `resourceIds` | arrays | yes | What is disrupted |
| `affectedPlans` | array | yes | `[{ planId, planVersion, replanRequested, replanOutcome }]` |
| `triggerEventIds` | array of UUID | yes | Every event folded into this record (dedup) |
| `quality` | enum | yes | Confidence in the classification |
| `createdAt` / `updatedAt` | timestamp | yes | |

## Required interfaces

### C05 findAffectedWork(changeScope) → Result\<AffectedWork\>

C07 asks C05 which unfinished work a change touches. C05 owns plans, so it
decides what "affected" means for a route; C07 only says where and what
changed. For the slice, C07 sends one call per changed segment.

> **Owner: C05.** The definition below is held here until C05 publishes it in
> its own `internal.md` (#32). Then this section keeps only the reference, and
> the examples move to the C05 folder.

Examples: `examples/find-affected-work-request.json`,
`examples/find-affected-work-response.json`.

**Request: changeScope**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `segmentKeys` | array of string | one of | Segments whose condition changed |
| `resourceIds` | array of UUID | one of | Resources whose availability changed or failed |
| `planIds` | array of UUID | one of | Plans named directly (for example by `OperationalDisruption`) |
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

**Failures:** `Invalid`, `Unavailable` (C07 then reports disruption status
`unknown`).

### C05 requestReplan(context, planId, trigger, key) → Result\<ReplanOutcome\>

C07 sends one `requestReplan` per affected plan. The call is synchronous: C05
answers with a final outcome straight away.

> **Owner: C05.** Held here until #32, as above.

Examples: `examples/request-replan.json`, `examples/replan-saved.json`.

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
| `status` | enum | yes | `NoChange` / `ReviewRequired` / `PlanSaved` |
| `newPlanVersion` | integer | when saved | Version of the saved revision |
| `decisionId` | UUID | when saved | C10 decision record |

**Failures:** `Conflict` (plan version moved on; C07 re-runs `findAffectedWork`
once), `Infeasible`, `Unavailable`, `NotFound`.

The idempotency key is what stops the C08 → C07 → C05 loop. The same
disruption version can never produce a second replan of the same plan; C05
returns the first outcome again.

### C06 getCitySnapshot(scope, freshnessPolicy) → Result\<CitySnapshot\>

Defined in `../c06-city-monitoring/internal.md`. C07 uses it when a
`CityStateUpdated` event does not carry enough context. Not used by the thin
slice.

## Decisions (16 Nov 2026)

- [x] Segment keys: `seg:<osm_way_id>` (OpenStreetMap way ID).
- [x] Default `maxAgeSeconds` for freshness: 900 s.
- [x] Material change that bumps a disruption version: status or severity changes, or new affected plans.
- [x] Severity rule for the slice: `closed` on a used segment = `high`; anything else is out of scope until Iteration 3.
- [x] `findAffectedWork`: one C05 call per changed segment for now.
- [x] `requestReplan` is synchronous; `PublicationPending` is dropped from `ReplanOutcome`.

## Still open

- [ ] `findAffectedWork` and `requestReplan` move to C05's contracts (#32).
- [ ] `ResourceAvailabilityChanged` (C03, #31) and `OperationalDisruption` (C08, #33) use a different envelope and payload from what C07 expects (#28).
- [ ] Events C07 publishes for C01 alerts and C10/C11/C12, if any.
- [ ] If C06/C07 take over UC12: does C12 replay read events from a C06/C07 history table or from a separate run log?
