# C05 Planning and Replanning

UC03 and UC07 contracts (#32), with the Dublin walking-skeleton implementation
tracked in [#118](https://github.com/ASE-Group-10/IntelligentUrbanPlatform/issues/118).
Pair Ye + Zecheng. Reviewers Bruno or Alejandro for C07 compatibility.

C05 owns proposals, confirmed plans and revisions. C08 owns actual allocation
and operational commitments. Planning reads those facts when checking feasibility.

## Conventions

Use camelCase JSON, UUID v4 identifiers, positive integer versions and UTC
RFC 3339 timestamps. Reuse the shared `Result<T>` from
`backend/app/shared/result.py`. A successful internal call returns `Ok(value)`;
a failure returns `Failure(kind, message)`. JSON examples show the success value.
`PlanningPort` returns this `Result` from both operations and raises no exceptions.

The adopted C07 models remain in
`backend/app/components/c07_disruption_detection/contracts.py` for compatibility.
`ChangeScope`, `AffectedWork`, `ReplanRequest` and `ReplanOutcome` are the current
typed models. `PlanningPort` in the adjacent `planning.py` is the current caller
interface. The agreements with C07 that shaped them are in
[Agreed with C07](#agreed-with-c07-59-20-nov-2026).

## Provided interfaces

### generatePlan(delivery, runScope, correlationId, key) → Result\<Plan\>

For hop 4 of [#72](https://github.com/ASE-Group-10/IntelligentUrbanPlatform/issues/72),
C04 should call `app.state.planning.generate_plan(delivery, run_scope, correlation_id, key)`
in-process after committing a delivery. C04 owns the authorised read of that
delivery in its run. Its [delivery contract](../c04-delivery-management/internal.md)
defines the saved fields. Wiring the C04 submit handler is pending.

| Argument | What C04 supplies |
| --- | --- |
| `delivery` | A `SavedDelivery` constructed from the committed [C04 delivery](../c04-delivery-management/examples/delivery-submitted.json). C05 reads `deliveryId`, `version`, `status`, `progress.status`, `cargo.weightKg`, `constraints.vehicleKinds` and `timeWindow`. |
| `run_scope` | A `PlanScope` with `scope="simulation"` and the delivery's UUIDv4 `runId`. |
| `correlation_id` | The UUIDv4 from the original delivery request, forwarded through C05 to C09 and C10 and retained for `PlanReady`. |
| `key` | A stable, nonblank key of at most 200 characters. The demo uses `c04:generate:{deliveryId}:v{version}`. |

The Dublin proposal uses the Eden Quay segment and one van with the saved cargo
weight. The delivery and its progress must both be pending, and its constraints
must allow a van. Departure is the later of the scenario start and the delivery
window start. The six-minute route must finish within that window. C05 queries
C09 for van delivery in Dublin D1/D2 during the saved window and passes the
returned evidence snapshot unchanged to C10. C09 reads the shared live policy
corpus and logs the same correlation ID. C10 records the simulation run and
the corpus version used for this decision. C10's `PlanGenerated` decision,
the C05 proposal and the generation receipt share one PostgreSQL transaction.
A write failure rolls all three back. The first `PlanReady` outbox entry belongs
to confirmation in hop 5.

`Ok(Plan)` returns the committed `planId`, `version=1`,
`status="awaiting_confirmation"` and `decisionId`. Retrying the same projected
delivery with the same caller, run and key returns that original version.
Different projected facts under the same key give `Conflict`. A signed-in
operator with access to the run can read the current version with
`GET /api/v1/plans/{planId}?scope=simulation&runId={runId}`.

#### Run hop 4 from the saved delivery example

From the repository root, use the local Compose stack and its migrated database.
The last command imports the saved example into C04's simulation store, commits
the delivery, then calls the C05 service with that delivery.

```sh
docker compose up -d --build
docker compose exec -T api python -m app.components.c09_logistics_knowledge.demo_corpus
docker compose exec -T api python -m app.demo.c05 < contracts/components/c04-delivery-management/examples/delivery-submitted.json
```

In PowerShell, use the following form for the last command.

```powershell
Get-Content -Raw contracts/components/c04-delivery-management/examples/delivery-submitted.json | docker compose exec -T api python -m app.demo.c05
```

The CLI prints the committed plan as JSON. Repeating it with the same inputs
returns the same plan and decision IDs. The defaults for `--run-id` and
`--correlation-id` come from the [Dublin fixture](https://github.com/ASE-Group-10/IntelligentUrbanPlatform/blob/develop/fixtures/dublin-scenario.json).
Pass the delivery's original values when replaying another trace. The end-to-end
check starts at C04's submit handler and requires evidence of its call to C05.

### Confirm a generated proposal (hop 5)

Open the `/plans?…` link printed by the generation command in the running
frontend. Sign in as an operator and select the acting provider. The page
loads the proposal for that run and displays **Confirm plan** above the vehicle
assignments. The URL carries `planId`, `scope`, `runId` and the original
`correlationId`.

The action sends `POST /api/v1/plans/{planId}/confirmation` with the saved
version in `expectedVersion`, the same `runScope`, and a stable
`Idempotency-Key`. C05 calls C04's `markRequestsPlanned` on the same PostgreSQL
connection. C04 checks each delivery version and records the link and its C10
audit. C05 saves the next confirmed version with its original decision ID, the
confirmation receipt and the first `PlanReady` outbox entry. These writes
commit together. A lost response can be recovered with the same key and
request. A conflicting version requires a fresh read.

The `c05-publisher` worker sends the committed envelope to the topic exchange
with routing key `PlanReady`. It marks the row published after RabbitMQ
confirms receipt. If that mark is lost, the worker sends the same event ID
again. The integration test receives the message and checks it against the
saved row and the C08 event contract. Run it from `backend/` with
`TEST_DATABASE_URL` pointing to a dedicated test database and
`TEST_RABBITMQ_URL` pointing to the test broker.

```sh
uv run pytest tests/test_c05_confirm_plan.py tests/test_c05_outbox_publisher.py
```

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
| `scope` | enum | yes | `live` or `simulation`, copied from the event that triggered the detection |
| `runId` | UUID | when simulation | Simulation run; must be absent when `scope` is `live` |

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
| `scope` | enum | yes | `live` or `simulation`, copied from the event that triggered the detection |
| `runId` | UUID | when simulation | Simulation run; must be absent when `scope` is `live` |
| `key` | string | yes | Idempotency key = `planId:basedOnPlanVersion:disruptionId:disruptionVersion` |

**Response: ReplanOutcome**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `status` | enum | yes | `NoChange` / `ReviewRequired` / `PlanSaved` |
| `newPlanVersion` | integer | when saved | Version of the saved revision |
| `decisionId` | UUID | when saved | C10 decision record |
| `reason` | string | when `ReviewRequired` | Why no feasible revision was saved, for the operator |

Example [review required](examples/replan-review-required.json).
An infeasible revision is not a failure: it is `Ok` with status `ReviewRequired`
and a `reason`.

**Failures:** `Invalid`, `NotFound`, `Conflict` and `Unavailable`. C07 treats any
`Conflict` as a stale plan; see [Agreed with C07](#agreed-with-c07-59-20-nov-2026).

The idempotency key is what stops the C08 → C07 → C05 loop. The same key always
returns the first outcome again. Because the key includes `basedOnPlanVersion`, C05
also returns the saved result when that disruption version already produced a
revision for that plan and run, even if a retry carries a newer `basedOnPlanVersion`.

## Required interfaces

| Provider | Operation | Use by C05 |
| --- | --- | --- |
| C02 | `authorize(context, action, scope)` | Authorise generation, confirmation and reads within the permitted scope |
| C03 | `listEligibleResources(scope)` | Read resource capabilities, observed availability and quality |
| C04 | `getRequests(context, runScope, requestIds)` | Read requirements, versions and progress in the authorised run |
| C04 | `markRequestsPlanned(context, runScope, requestIds, planId, expectedVersions, key)` | Link deliveries when confirming a plan |
| C06 | `getCitySnapshot(scope, freshnessPolicy)` | Read versioned and quality-labelled city conditions; definition in [C06](../c06-city-monitoring/internal.md) |
| C09 | `queryKnowledge(context, policyQuery)` | Obtain applicable policy evidence and provenance |
| C10 | `recordDecision(conn, ctx, record)` | Save actual input versions, alternatives, reasons and exact evidence on the C05 transaction's connection |

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

C08 uses `planId`, `planVersion`, `deliveryId` and the event's run scope to
look up the route and `plannedDepartureAt` together from that saved plan
version. The timestamp includes `Z` or a UTC offset. C08 separately rechecks
that the event version is still current before committing resources. The
`requirements` list describes all resource needs for
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

## Agreed with C07 (#59, 20 Nov 2026)

1. **Run scope.** `ChangeScope` and `ReplanRequest` carry `scope` (`live` or
   `simulation`) and `runId`, copied from the event that triggers the detection.
   `runId` is required for `simulation` and must be absent for `live`, the same
   rule as `EventEnvelope`. A simulation resolves only its run's plans, inputs and
   receipts; an unspecified run is never treated as live.
2. **Idempotency key.** `planId:basedOnPlanVersion:disruptionId:disruptionVersion`.
   The key does not include the run scope; C05 scopes its receipts by run.
3. **Conflict.** C07 treats any `Conflict` from `requestReplan` as a stale plan and
   retries once; if C05 needs to report key reuse differently, it should use a
   distinct failure. The retry is: call `findAffectedWork` once more (all the
   disruption's `segmentKeys`, same `scope` and `runId`), then send one new
   `requestReplan` with the `planVersion` C05 now reports, which gives a new key.
   C07 does not retry if the plan is no longer affected, or if C05 reports the same
   `planVersion` as before (the key would be identical). A second failure leaves
   the replan not made. C05, for its part, returns the saved result when that
   disruption version already produced a revision for that plan and run.
4. **ReplanOutcome.** It gains `reason` (text, optional), required when `status` is
   `ReviewRequired`. `Infeasible` is no longer a failure. The remaining failures are
   `Invalid`, `NotFound`, `Conflict` and `Unavailable`.
5. **PlanSaved.** It is returned when the revision and its `PlanReady` outbox entry
   are saved together. C05 owns the publication state; it is not part of the
   response to C07.
6. **Port.** `PlanningPort` returns a typed `Result` (`app.shared.result`: `Ok` or
   `Failure`) from both calls, instead of raising exceptions.
7. **Unavailable and retry.** C07 marks the detection result as `retryable` only
   when delivering the event again can change the outcome:
   - If C05 answers `Unavailable` to `findAffectedWork`, C07 reports `unknown` and
     does not mark the event as processed, so the event is not lost.
   - If a `requestReplan` is not made because C05 answered `Unavailable`
     (including the rediscovery after a `Conflict`), C07 keeps the disruption with
     that plan as not requested and the result is `retryable`. When the event
     arrives again, C07 sends only the plans still pending, with the same key, so
     C05 deduplicates on it. A pending plan whose version has moved on still
     follows the `Conflict` rule of point 3.
   - Causes a redelivery would not change do not set `retryable`: `NotFound`, a
     plan that is no longer affected, a repeated `Conflict` or one with the same
     `planVersion`, and `Invalid`. Those plans stay not requested, so the event is
     not retried forever.

## Open coordination points

- C04 and C01 coordinate the hand-off of the saved proposal ID and version from
  delivery submission to the Plans page. The generation path for #118 is the
  in-process call above. The `POST /api/v1/plans` entry in the UC03 OpenAPI
  draft describes a separate HTTP operation.
- Exchange/routing and the canonical shared event envelope are tracked in #28.
  The event draft refers to the existing C06 envelope to avoid another copy.
- C05 receives replanning through an internal call from C07. No direct subscription
  to `CityStateUpdated` is currently proposed.
