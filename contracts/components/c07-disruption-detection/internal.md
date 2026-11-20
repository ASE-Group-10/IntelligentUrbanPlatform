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

### C05 planning operations

C07 requires `findAffectedWork(changeScope)` and the synchronous, idempotent
`requestReplan(context, planId, trigger, key)`. C05 owns their definitions and
examples in [C05 internal interfaces](../c05-planning/internal.md).
C07 uses the returned plan versions and final outcomes to update its disruption
record. The proposed discovery retry on a version conflict is listed in the
[C05 coordination points](../c05-planning/internal.md#open-coordination-points).

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

- [x] `findAffectedWork` and `requestReplan` definitions and examples are owned by C05 (#32).
- [ ] Align the C03 producer envelope and payload for `ResourceAvailabilityChanged` with the C07 consumer (#31).
- [ ] Review the C08 `OperationalDisruption` contract and failure kinds with C07 (#33). The C08 fixture passes C07 consumer validation.
- [ ] Events C07 publishes for C01 alerts and C10/C11/C12, if any.
- [ ] If C06/C07 take over UC12: does C12 replay read events from a C06/C07 history table or from a separate run log?
