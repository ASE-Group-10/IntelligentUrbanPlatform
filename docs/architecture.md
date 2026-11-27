# Intelligent Urban Logistics Optimisation Platform — Architecture (Group 10, CS7CS3)

Version 1.0 (agreed architecture text), 5 Nov 2026. Intended design only: no deployment, benchmark, provider connection or recovery test has been performed. Scope: Dublin, synthetic demonstration data.

For implementation, the current component contracts in `contracts/` take precedence wherever they differ from this architecture snapshot.

---

## 1. Core architectural principles (use as constraints in any prompt)

1. **12 logical components (C01–C12)** in layers; layers group responsibilities, not deployment processes. Components may share a process/DB transaction but keep separate ownership. The **event bus is shared infrastructure**, not a 13th component.
2. **Humans/short queries → authenticated APIs. Observations, saved-plan notifications, agent proposals, operational exceptions → event bus.**
3. **Planning status ≠ execution status.** C05 owns the planning lifecycle; C08 owns allocation/execution. A saved valid plan does NOT imply resources confirmed, driver accepted, or instruction received.
4. **C05 never calls C08.** C05 saves plan + `PlanReady` outbox event; C08 consumes it asynchronously.
5. **Driver acceptance belongs exclusively to UC04.** UC08 never prompts or waits for a driver.
6. **No restart loops.** UC08 has no AF4 restart. Failures emit `OperationalDisruption` → C07 → (maybe) separate `requestReplan` to C05. Path: `C03/C08/connector → event bus → C07 → C05`.
7. **Idempotency + versioning:** every command carries an idempotency `key`; expected-version checks prevent stale writes; consumers deduplicate events; transport retries resend the same event (not new planning attempts).
8. **Transactional outbox:** state + durable decision record/evidence + outbox entry commit together. Broker confirmation = broker receipt only.
9. **C10 owns historical evidence** (immutable decision-time snapshots). C09 has **no getEvidence API**; explanations/replay never call C09.
10. **Replay has no live side effects** (isolated credentials/run namespace; cannot publish commands or mutate live plans).
11. **C02 protects all APIs** (default deny, role+scope checks, identifiers alone never grant access).
12. Missing data is shown as unavailable/unknown, never zero/invented. Missing evidence is identified, never reconstructed from today's knowledge.

### Functional layers
- **External:** Mapping Service, Users/Operators, Traffic/Weather/Events feeds, Logistics-provider APIs
- **Presentation:** C01 Web Interface, C02 Identity & Access
- **Core domain:** C03 Resource Mgmt, C04 Delivery Mgmt, C05 Planning & Replanning, C08 Allocation & Resource Agents
- **Intelligence/knowledge:** C06 City Monitoring, C07 Disruption Detection, C09 Logistics Knowledge, C10 Decision Records & Explanation, C11 KPI Analytics, C12 Simulation & Replay
- **Integration:** Event Transport Bus (RabbitMQ)

Key flows: C05 reads C03/C09 (and C06 snapshot); C08 reads saved plan/requirements from C05; C05 → bus `PlanReady` → C08; C03 resource changes + C08 failures → bus → C07; C07 → `requestReplan` → C05; C05/C08 record evidence in C10; C01/C12 retrieve explanations from C10.

---

## 2. Components

| ID | Component | Responsibilities / owned info | Key boundaries |
|---|---|---|---|
| C01 | Web Interface | React forms, map, operator plan confirmation, UC04 driver response, alerts, curator tools, explanations, KPI views, replay controls; transient view state | Presentation only; server is authoritative |
| C02 | Identity & Access | Authenticate users/services; authorise manager, operator, driver, analyst, curator, administrator; record access context | Replaceable provider adapter (Supabase Auth default); cross-provider commercial sharing out of scope |
| C03 | Resource Management | Register/validate vehicles, depots, warehouses, lockers; profiles, location, capability, capacity, observed availability, constraints, agent activation status | Owns resource profiles/status; C08 owns allocation commitments; unvalidated/agent-unavailable resources are not planning-eligible |
| C04 | Delivery Management | Validate/persist delivery requests, locations, cargo, constraints, time windows; dedupe; expose request facts/progress | A recorded request ≠ feasible plan or confirmed assignment |
| C05 | Planning & Replanning | Feasible routes, schedules, mode/facility choices, resource requirements; compare alternatives; own proposed/confirmed/revised plan versions; save decision evidence | UC07 stops at saved valid plan + `PlanReady`. Doesn't invoke C08, reconcile allocations or dispatch. Completed deliveries unchanged. Uses C06 current conditions + C09 historical info. Checks vehicle/facility capacity, goods location, handover feasibility; loaded goods stay with vehicle until C08 confirms transfer |
| C06 | City Monitoring | Ingest real/simulated observations + manual reports, normalise to Dublin segments/zones, own City State + history, freshness/conflicts, publish changes | **Only C06 writes City State**; supplies conditions, C07 evaluates disruption |
| C07 | Disruption Detection | Consume city/resource/operational-failure events, classify impact, find affected unfinished work, create/update disruption records, request separate replan per affected plan | No route solving or resource commitment; ignores duplicates/irrelevant events; failed attempts don't self-trigger endlessly |
| C08 | Allocation & Resource Agents | Resource-agent proposal logic, negotiation, allocation authority, UC04 vehicle offers/acceptance, operational instructions, receipt tracking; owns reservations/allocations/execution status; agents represent synthetic providers | UC08 has no driver acceptance; unexpected invalidation emits event rather than restarting UC08 |
| C09 | Logistics Knowledge | Curator publication, versioned current corpus, semantic search, applicability filtering; returns content, provenance, conflict warnings | Published curator content is trusted; no historical evidence lookup |
| C10 | Decision Records & Explanation | Persist decisions, input versions, evidence snapshots, considered alternatives, reasons, estimates, outcomes; generate/serve explanations and historical evidence; audit (`recordOutcome`) | No runtime C09 dependency; missing evidence flagged, never invented |
| C11 | KPI Analytics | Operational read models; delivery time, cost, congestion, environmental metrics with units/scope/coverage/timestamps/estimation status | Read-only; formulas, weights, emission/cost coefficients configured later |
| C12 | Simulation & Replay | Reproducible simulated scenarios/autonomous vehicles; record initial state, ordered events, decisions, results; replay with timeline controls | Simulation and live modes separated; replay cannot issue live instructions |

---

## 3. Ownership & coordination rules

- **Plan vs execution:** If a previous plan is in execution, C08 retains its valid commitments until a subsequent operational change is committed. A rejected candidate revision never overwrites operational history. Track planning status and execution status separately.
- **UC03 vs UC07 notifications:** UC03 requires operator confirmation before the plan is available for assignment; UC07 publishes its checked revision as ready. Both emit `PlanReady` with plan ID, version, request/resource needs, decision ID, idempotent event identity.
- **UC08 internals:** resource agents evaluate proposals; allocation authority commits compatible outcomes. Allocation confirmation + agent acknowledgement can complete negotiation (acknowledgement = receipt of coordination result, not driver acceptance). UC04 reuses the negotiated commitment (no double allocation). Driver rejection is handled in UC04; unresolved downstream effects use the disruption-event path.
- **Transactions:** short DB transactions with version/capacity checks. C03/C04/C05/C08/C10 write modules may share the Python core + Supabase DB. C05's plan-save transaction is separate from a later C08 commitment; still must reject incompatible commitments.
- **C08 commit-time:** atomic capacity + expected-version checks. On failure: don't report success, don't send executable instruction; store failed outcome + outbox event; C07 decides about replan.
- **C07 dedup:** by event ID and relevant plan/disruption version. Repeated processing of same failure doesn't trigger another replan without a materially changed trigger or authorised new request.

### Decision evidence & knowledge trust
- `queryKnowledge` returns actual passages/facts, source/document/section IDs, document version, applicability/effective dates, retrieval/model/index metadata.
- Decision maker passes this **EvidenceSnapshot** to `C10.recordDecision` with input versions, selected option, considered alternatives, reasons. Needs durable C10-owned snapshot or immutable C10-managed blob — **a reference to C09's mutable index is insufficient**.
- Curator publication activates corpus immediately (no second approval). Conflicts: return applicable trusted passages + warn curator; warnings don't block plans. Planner still enforces hard constraints; mandatory-constraint conflicts with no feasible plan → `Infeasible` (no invented resolution). Low retrieval confidence is displayed/logged as limitation.
- Explanations: use saved decision + evidence snapshot, distinguish predicted vs observed effects, cite recorded sources. LLM outage → structured explanation from same record.

---

## 4. Use-case interactions (UC01–UC12)

**Common protected operation:** C01 gets AuthContext from C02 → target service calls `authorize(context, action, scope)` → validates → records outcome in C10 → returns typed result with correlation ID. Denied → `Forbidden` without restricted details.

**UC01 Register resource** — `registerResource` (C03): validate fields, check provider/resource identity, validate location via mapping, persist, call `activateAgent` (C08), return actual eligibility.
- AF1 invalid fields → correct/resubmit; AF2 duplicate → cancel/view/update; AF3 unconfirmed location/restriction → correct or `savePendingResource` (planningEligible=false); AF4 agent activation fails → `RegisteredAgentUnavailable`, ineligible until `retryAgentActivation` succeeds.

**UC02 Submit delivery** — `submitDelivery` (C04) with pickup/destination, cargo, time, constraints, idempotency key. Validates fields + mapped locations, stores, returns ID. AF1 invalid → field feedback (not stored); AF2 storage uncertain → `Unconfirmed(correlationId)`; retry with same key returns existing result. No route/vehicle implied.

**UC03 Generate plan** — C05 reads validated requests (C04), eligible resources (C03), quality-labelled City Snapshot (C06), trusted knowledge (C09); computes feasible alternatives, scores by configured metrics, records evidence in C10, saves awaiting-confirmation proposal with estimated KPIs. Operator `confirmPlan` atomically confirms plan, links requests (`markRequestsPlanned`), enqueues `PlanReady` in outbox; published after commit; retries reuse event without replanning. Proposal contains vehicle *requirements*, no specific vehicle.
- AF1 no candidate satisfies hard constraints → `Infeasible(conflicts)`, requests stay pending; operator may change requirements → fresh attempt. AF2 essential data unavailable → `DataUnavailable`, visible failure; retry is a separate request.

**UC04 Assign vehicle** — `assignVehicle` (C08): `getPlan` (C05), `listEligibleResources` (C03), loop over bounded candidates: `rankCandidates` → `reserveCandidate` → offer to driver/simulated vehicle → `recordOfferResponse` → `commitAssignment(assignmentRef, expectedPlanVersion, key)`.
- AF1 no suitable vehicle → `Unassigned(reason)`, recommend rescheduling/external provider. AF2 vehicle becomes unavailable before confirmation → release uncommitted reservation, publish `OperationalDisruption`, return `Unresolved` (doesn't wait for C07). AF3 driver rejects → record, release reservation, next candidate. AF4 (optional) resource conflict → invoke UC08 `negotiateAllocation`; reuse its vehicleId/allocation ref/plan version; unresolved negotiation ends attempt. Allocation reference finalised after acceptance **without allocating capacity again**. State/decision/outbox commit atomically; dispatch only after valid commit + acceptance.

**UC05 Monitor city** — `ingestObservation` (C06): authenticate, normalise units/times/segment IDs, store raw + normalised history, merge into new City State version, durable outbox → `CityStateUpdated(snapshotVersion, previousVersion, changes, degraded, staleSources)` to C07 and views. Ack after persistence.
- AF1 auth/schema failure → reject, alert on repeated errors; AF2 stale feed (scheduled `checkFreshness`) → mark stale, labelled historical fallback, degraded confidence, stale alert; AF3 conflicting sources → keep both, precedence/quality metadata, lower confidence, flag; AF4 older observation → history only; AF5 manual report → authorised roles only, author + expiry; AF6 broker unavailable → durable outbox, backoff retry, publish every pending event in order on recovery so consumers see each version.

**UC06 Detect disruption** — C07 consumes `CityStateUpdated`, `ResourceAvailabilityChanged`, `OperationalDisruption`; validates quality (reads C06/C03 fallback), `findAffectedWork` (C05), `classifyImpact`, `upsertDisruption`, and for new/materially changed impact issues one `requestReplan` per affected plan.
- AF1 invalid/stale/unavailable data → bounded fallback with quality limits, else "status unknown"; AF2 no relevant disruption → `NoDisruption`; AF3 matches existing → update. Duplicate/unchanged events ignored.

**UC07 Replan** — `requestReplan` (C05), one attempt: reads current plan version, requirements/progress, resources, City State, knowledge. Confirmed allocation facts are read-only snapshot inputs (constrain feasibility; not changed). Produces routes, schedules, resource requirements for unfinished deliveries only.
- `NoChange` (no affected unfinished work); AF1 infeasible → `ReviewRequired(conflicts)`; AF2 missing/unreliable inputs → `ReviewRequired(reason)`; AF3 save failure/version conflict → preserve latest plan, `ReviewRequired`, don't publish.
- `saveRevision(planId, expectedVersion, revision, key)`: one transaction saves revision + C10 decision (exact evidence) + `PlanReady` outbox entry. Result `PlanSaved` (`awaitingExecution`). Retry doesn't rerun solver. No C08 calls, reconciliation, dispatch, or waiting for receipts.

**UC08 Negotiate allocation** — C08 PlanReady handler calls `negotiateAllocation`: `queryKnowledge`, `requestProposals(negotiationId, needs, deadline, inputVersions)` to agents (bounded window), agents `evaluateProposalRequest` → `submitProposal`; `rankCandidates` via metric scoring; `commitNegotiation(negotiationId, selectedProposal, expectedVersions, key)`.
- AF1 no feasible proposal → `AllocationFailed`, operator-visible exception; AF2 equivalent scores → documented emissions/fairness/workload criteria + stable tie-break; AF3 agents don't respond by deadline → log, continue with available agents. **No AF4.**
- Commit fails/rejected → record failure + durable `OperationalDisruption` outbox event, return `AllocationFailed`, preserve prior state, no confirmation/instruction. Success → committed allocations + resulting plan/resource versions; driver work marked `AwaitingUC04Acceptance`; executable driver instructions withheld until UC04 done. Missing ack by deadline → `receiptStatus=Pending` (commitment stays); `reconcileDispatch` retries same notification, not negotiation.

**UC09 Query knowledge** — `queryKnowledge(context, policyQuery)`: semantic search, validity/scope filtering, rerank, provenance assembly. Results: sufficient evidence / `LowConfidence` / `Conflict` (warn curator via `notifyKnowledgeConflict`, no approval wait). `publishKnowledge` (curator-only) validates, versions, indexes, activates corpus. Caller records evidence via `C10.recordDecision` whenever a decision proceeds (including with flagged results).

**UC10 Explain decision** — `explainDecision` (C10): `getDecision` → `loadStoredEvidence` → generate grounded explanation. AF1 missing/ambiguous/inaccessible → candidate list without restricted details; AF2 incomplete evidence → `PartialExplanation(supportedFacts, explicitGaps)`; AF3 generator/evidence failure → `StructuredFallback` or `TemporarilyUnavailable`. `getDecisionEvidence`/`loadDecisionEvidence` are C10-internal helper reads (no C09). Never mutates original decision/plan; predicted vs observed separated.

**UC11 Monitor KPIs** — `queryKPIs(context, scope, metricDefinitionVersion)`, `getKPIBreakdown`. AF1 insufficient data → `PartialKPIs` (unavailable, never zero); AF2 source unavailable → `Stale(lastSuccessfulValues, lastUpdated, failure)`. Read-only.

**UC12 Replay** — `listRuns` → `startReplay(runId, range, speed)` → `controlReplay` (play/pause/resume/seek/stop) → `advanceReplay(sessionId, expectedCursor)` → `ReplayFrame`. AF1 unreconstructable range → `CannotReplay(reason, availableRanges)`; AF2 interrupted reads → pause at last cursor, resume without skipped/duplicate events. Explanations use run-scoped C10 records. Never reruns present-day optimisation.

---

## 5. Component APIs (logical contracts, not necessarily HTTP endpoints)

**Shared types:** `AuthContext` (principal/service identity, roles, scope, correlation ID). `Result<T>` = success or `Invalid | Forbidden | NotFound | Conflict | Unavailable | Unconfirmed | Infeasible | Partial`. Plan generation returns `NoChange | ReviewRequired | PlanSaved` — never allocation-confirmed. `PolicyQuery` (area, time/window, vehicle class, activity, optional context). `KnowledgeResult`, `EvidenceSnapshot` (exact content + identifiers/versions, persisted by C10). `EventEnvelope` (event ID, schema/type, producer, observed/ingested times, correlation/causation IDs, affected plan/resource IDs, entity versions, live/simulation run scope). `PlanReady` (saved plan version + resource needs). `OperationalDisruption` (failure type, affected entities, versions, decision ID, last confirmed state, unresolved work).

| Comp | APIs |
|---|---|
| C01 | `openView`, `validateForm` (client-side feedback only), `renderResult`, `subscribeView(scope, lastSeenEventId)` |
| C02 | `authenticate(credentials)`, `authorize(context, action, scope)` (default deny) |
| C03 | `validateResource`*, `registerResource`, `getResource` (also C07 fallback), `updateResource(version)`, `savePendingResource`, `retryAgentActivation`, `listEligibleResources` (not a capacity commitment), `updateResourceStatus` (emits `ResourceAvailabilityChanged` via outbox) |
| C04 | `validateDelivery`*, `submitDelivery`, `storeDelivery`*, `updateDelivery(version)`, `getRequests`, `markRequestsPlanned`* |
| C05 | `generatePlan`, `computeCandidate`*, `saveProposal`, `confirmPlan`, `getPlan`, `findAffectedWork`, `requestReplan`, `saveRevision(expectedVersion)`, `publishPlanReady`* |
| C06 | `ingestObservation`, `getCitySnapshot(scope, freshnessPolicy)`, `submitManualCondition`, `checkFreshness`, `publishCityChanges` |
| C07 | `evaluateChange`, `classifyImpact`*, `upsertDisruption` |
| C08 | `activateAgent`, `handlePlanReady`, `assignVehicle`, `rankCandidates`, `reserveCandidate`*, `recordOfferResponse`, `releaseReservation`, `commitAssignment`, `negotiateAllocation`, `requestProposals`, `evaluateProposalRequest`, `submitProposal`, `commitNegotiation`, `dispatchPlanUpdates`, `reconcileDispatch`, `acknowledgeInstruction`, `publishOperationalDisruption` |
| C09 | `queryKnowledge`, `publishKnowledge` (curator-only), `notifyKnowledgeConflict` |
| C10 | `recordOutcome`, `recordDecision`, `getDecision`, `explainDecision`, `getDecisionEvidence` |
| C11 | `consumeOperationalEvent`, `queryKPIs`, `getKPIBreakdown` |
| C12 | `startSimulation(scenario, seed, configurationVersion)`, `recordSimulationEvent`, `listRuns`, `startReplay`, `controlReplay`, `advanceReplay` |

\* internal. Notable semantics: `commitAssignment` accepts reservation ref or existing negotiated allocation ref, checks capacity/expected plan version/UC04 acceptance, finalises without re-allocating; `releaseReservation` never silently cancels confirmed allocations; `recordOfferResponse` is the only place driver acceptance is stored; `submitProposal` validates agent identity/scope/input version/expiry.

### External connectors & event contracts
- **Mapping/location:** HTTPS adapter (Dublin locations, coordinates, road network, travel estimates). C04 calls `validateLocations(pickup, destination)` → matched / unmatched / unconfirmed; store only when both validated.
- **Traffic/weather/events:** HTTPS pull/push adapters into C06; preserve source time, units, area, quality, quotas.
- **Logistics-provider:** adapter for resource/delivery status and authorised operational updates via C03/C04/C08; dedupe provider IDs/messages; credentials/API availability are external dependencies.
- **Browser:** HTTPS JSON commands/queries; authenticated SSE or polling for views; reconnect via scoped snapshot/version.
- **Events:** versioned JSON `EventEnvelope`. `PlanReady` → C08; `OperationalDisruption` / `ResourceAvailabilityChanged` / `CityStateUpdated` → C07; outcomes → C10/C11/C12.

---

## 6. Technical architecture

**Stack:** React/TypeScript frontend (Vercel, or AWS/Azure). Python/FastAPI application core + separate Docker worker containers on AWS or Azure. RabbitMQ (AMQP over TLS :5671, durable queues/acks) as persistent backend service. Supabase PostgreSQL (+PostGIS, pgvector, Auth, Storage or cloud object store). OpenTelemetry + Datadog or Grafana. Docker Compose locally, versioned DB migrations, CI (pytest, TypeScript checks, image build).

**Deployment placement**
- Public ingress (Nginx/load balancer, TLS termination) → private network.
- *Application core container:* C02 adapter, C03, C04, C05 plan state/APIs, C08 allocation authority, C10 records/evidence/explanation API.
- *Workers:* planning/resource agents (C05 route computation, C08 proposals); city monitoring/disruption (C06, C07); knowledge retrieval (C09, embedding API); explanation generation (C10, LLM API, saved evidence only); analytics/simulation/replay (C11, C12, isolated, no live command access).
- *Data:* Supabase Postgres (domain state, city history, decisions/evidence, corpus/run records); Storage/object store (C09 sources, C10 evidence blobs, C12 recordings); **independent** encrypted S3/Azure Blob for daily DB + object backups.
- **Do not** run backend/RabbitMQ consumers as Vercel serverless functions (time-bounded, stateless; can't hold persistent AMQP or run heavy solvers). Vercel = frontend only.
- Cloud choice doesn't change the 12 logical components. Identity: Supabase Auth default; role checks stay in Python APIs. LLM/embedding vendor is interchangeable; model/provider IDs recorded.

### QoS / non-functional requirements (design objectives, not measurements)
- **Security:** HTTPS/TLS everywhere, encrypted backups, secret management; roles (operator, manager, driver, analyst, curator, admin); audit log (actor, action, decision ID, entity versions, result, timestamp; no secrets) — architectural policy, not legal-compliance claim; public ingress separated from private backend/DB/broker; broker ACLs, schema validation; retrieved document text is data, not instructions; send only permitted data to generation provider.
- **Performance/targets:** UC09 query answer ≤ 1 s; successful ingestion available ≤ 5 min (against configured corpus/workload). **No fixed UC05 throughput/publication targets** (removed; measure observed workload). Record planning/negotiation/generation timings; return job/status handles for long operations.
- **Availability/recovery:** target availability for operational/evaluation sessions; planned maintenance allowed; container supervision + persistent state; no zero-downtime or % guarantee. Daily encrypted DB/object backups; 30 daily, 12 weekly, project-lifetime monthly; **RPO 24 h, RTO 4 h**; monthly isolated restore tests. (Supabase DB backups exclude Storage objects and aren't independent of the project.)
- **Failure handling:** health/readiness checks, connector isolation, broker/DB alarms, stale/partial UI states, durable pending events.
- **Client:** Chrome/Firefox desktop + responsive driver view; English, consistent units, explicit local/UTC timestamps; browser holds only view state, server owns requests/plans/commitments; area-filtered map data, paginated history; after disconnect, reload authoritative assignment/instruction status before further actions.
- **Network/data:** backend + Supabase region near Dublin (nearby European region; actual region recorded in deployment config); batch/index/archive history; measure external delay, queueing, compute and receipt separately; backoff/rate-limit handling for provider quotas.
- **Server/limits:** least-privilege credentials, parameterised queries, protected management endpoints, request/job limits; profile ingestion, planner, proposal fan-out, generation, DB separately; scale workers by measured bottleneck.
- **Connectors:** real Dublin mapping/traffic/weather/logistics APIs required; synthetic operational records for evaluation; per-source freshness; knowledge cache invalidated on curator publication; C10 historical evidence is immutable (not a cache); offline status never implies confirmed allocation.
- **Known limitation:** C08 agents run in cloud containers, not on physical vehicle/facility edge hardware (deliberate demonstrator scope boundary vs project brief). No physical autonomous-vehicle controller; simulated AV telemetry.
- **Out of scope:** cross-provider resource-sharing policy, real customer-data handling, detailed KPI formulas/score weights/emission-cost coefficients (later configuration).

---
