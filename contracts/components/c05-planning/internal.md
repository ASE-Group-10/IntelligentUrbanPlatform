# C05 Planning and Replanning

Contract draft for UC03 and UC07, issue #32. Pair Ye + Zecheng.
Reviewers Bruno or Alejandro for C07 compatibility. Proposed HTTP and event
fields require review with C01, C08 and C10 before implementation.

C05 owns proposals, confirmed plans and revisions. C08 owns actual allocation
and operational commitments. Planning reads those facts when checking feasibility.

## Conventions

Use camelCase JSON, UUID v4 identifiers, positive integer versions and UTC
RFC 3339 timestamps. Reuse the shared `Result<T>` from
`backend/app/shared/result.py`. A successful internal call returns `Ok(value)`;
a failure returns `Failure(kind, message)`. JSON examples show the success value.
The existing `PlanningPort` returns unwrapped values and raises
`PlanningUnavailableError` for unavailability. Its future C05 adapter must map
these to and from the logical `Result` contract.

The adopted C07 models remain in
`backend/app/components/c07_disruption_detection/contracts.py` for compatibility.
`ChangeScope`, `AffectedWork`, `ReplanRequest` and `ReplanOutcome` are the current
typed models. `PlanningPort` in the adjacent `planning.py` is the current caller
interface. This documentation migration preserves their fields and imports.

## Provided interfaces

### findAffectedWork(changeScope) → Result\<AffectedWork\>

C07 asks C05 which unfinished work a change touches. C05 owns plans, so it
decides what "affected" means for a route; C07 only says where and what
changed. For the slice, C07 sends one call per changed segment.

Examples [request](examples/find-affected-work-request.json) and
[response](examples/find-affected-work-response.json).

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

### requestReplan(context, planId, trigger, key) → Result\<ReplanOutcome\>

C07 sends one `requestReplan` per affected plan. The call is synchronous: C05
answers with a final outcome straight away.

Examples [request](examples/request-replan.json),
[saved outcome](examples/replan-saved.json) and
[conflict failure](examples/replan-conflict.json).

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

The inherited logical draft lists `Conflict`, `Infeasible`, `Unavailable` and
`NotFound`. The current `PlanningPort` returns an unwrapped `ReplanOutcome` or
raises `PlanningUnavailableError`. C07 catches that exception and otherwise
records `outcome.status`. Agree the mapping of other logical failures and any
`Conflict` discovery retry with C07 before connecting the C05 adapter.

The idempotency key is what stops the C08 → C07 → C05 loop. The same
disruption version can never produce a second replan of the same plan; C05
returns the first outcome again.

## Required interfaces

| Provider | Operation | Use by C05 |
| --- | --- | --- |
| C02 | `authorize(context, action, scope)` | Authorise generation, confirmation and reads within the permitted scope |
| C03 | `listEligibleResources(scope)` | Read resource capabilities, observed availability and quality |
| C04 | `getRequests(context, requestIds)` | Read requirements, versions and progress |
| C04 | `markRequestsPlanned(requestIds, planId)` | Link deliveries when confirming a plan |
| C06 | `getCitySnapshot(scope, freshnessPolicy)` | Read versioned and quality-labelled city conditions; definition in [C06](../c06-city-monitoring/internal.md) |
| C09 | `queryKnowledge(context, policyQuery)` | Obtain applicable policy evidence and provenance |
| C10 | `recordDecision(decisionRecord)` | Preserve actual input versions, alternatives, reasons and exact evidence used |

C02/C03/C04/C09/C10 provider definitions remain authoritative as their contracts
are published. Required operations listed here do not define their fields.

## Planning and persistence rules

The proposed C01 HTTP operations are in `openapi.yaml`. Authentication is obtained
from the bearer token. HTTP paths use `/api/v1/plans`; HTTP errors use the
C11/C12-style `error { code, message, correlationId }` envelope proposal, pending
shared frontend review. This transport mapping does not change the internal
`Result` contract or C07 models. Authorisation and scope checks use C02.
An initial proposal awaits operator confirmation. Confirmation checks its expected version, links its
deliveries and queues `PlanReady` atomically with the confirmed state and decision
references. A saved revision atomically records its expected-version update,
C10-owned decision/evidence and `PlanReady` outbox entry.

C10 evidence contains the exact passages and source/version/provenance used at
decision time. Evidence is retained after the C09 corpus changes. An outbox
publisher retries the saved event ID without rerunning planning. Failure to commit
any authoritative state, evidence or outbox entry rolls back the operation.

For UC07, `trigger.citySnapshotVersion` records the City State that triggered
the request when C07 provides it. C05 reads current conditions in the same run
and records the version it actually uses for the decision.

C08 uses `planId`, `planVersion` and the event's run scope to look up C05's
current plan. It rechecks that the event version is still current before
committing resources. The `requirements` list describes all resource needs for
unfinished deliveries in that plan version. C08 checks which existing
commitments still satisfy those needs against the revised route and timing.

For mutating HTTP calls, a retry with the same caller, operation, run scope, key
and request returns the first committed response. Reusing that key with a
different request gives `Conflict`. Concurrent writes use expected-version checks.
A lost response after commit is recovered by retrying the same request and key.

## Example sequence

The [proposal](examples/plan-proposal.json) at version 2 is confirmed with
[expectedVersion 2](examples/confirm-plan-request.json), giving the
[confirmed plan](examples/confirmed-plan.json) at version 3. The earlier version
1 is outside this excerpt. After C06 records snapshot 42, C07 requests a replan
based on version 3. The [saved outcome](examples/replan-saved.json) refers to
[revision 4](examples/revised-plan.json), which avoids `seg:4471023`.
Versions 3 and 4 have the same van capacity requirement. The
[PlanReady event](examples/plan-ready-revision-event.json) carries that requirement
with version 4 and its decision ID. It retains the closure correlation ID and
simulation run ID and has its own event ID.
HTTP examples and this event illustrate proposed fields pending consumer review.

## Open coordination points

- The HTTP paths, proposal representation, priorities and resource-needs schema
  are proposals for C01/C03/C04/C08 review. Only the two C07 interfaces above
  have existing typed models. New HTTP/event schemas have no runtime yet.
- The existing `PlanningPort` passes no run scope or run ID to either method.
  Agree scope propagation with C07 before connecting simulation to real C05
  storage. A simulation must resolve only its run's plans, inputs and receipts.
  Do not silently treat an unspecified simulation run as live.
- Agree with C07 whether one fresh discovery is allowed after a stale-plan
  `Conflict`, and whether that conflict consumes the key. A refreshed
  `basedOnPlanVersion` changes the request but leaves the current key unchanged.
- The inherited logical draft lists `Infeasible`, while architecture UC07 returns
  `ReviewRequired` for an infeasible revision. Agree that mapping with C07. The
  current `ReplanOutcome` has no reason/details field.
- Agree with C07 when the synchronous `PlanSaved` outcome is returned and how to
  report a saved revision while `PlanReady` publication is pending. `PlanReady`
  is the event for confirmation and saved revisions.
- Exchange/routing and the canonical shared event envelope are tracked in #28.
  The event draft refers to the existing C06 envelope to avoid another copy.
- C05 receives replanning through an internal call from C07. No direct subscription
  to `CityStateUpdated` is currently proposed.
