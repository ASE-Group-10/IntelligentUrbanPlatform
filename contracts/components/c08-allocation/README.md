# C08 Allocation and Resource Agents: UC04 and UC08

Owners: Shixian Cai and Óscar Gómez González. Status: proposal for pair and
consumer review; these documents do not implement a service.

Architecture: `Architecture_Group10_overleaf.pdf`, §§1.2.1, 1.3.5 (UC04,
Figure 6), 1.3.9 (UC08, Figure 10), 1.4.9 and 1.4.14.
Conventions: [contracts README](../../README.md).

## Scope and files

| Scope | Responsibility | Contract |
| --- | --- | --- |
| **UC04 Assign vehicle** | Select/reserve a vehicle, obtain driver acceptance, commit one assignment and expose its actual state | [HTTP](openapi.yaml), [internal](internal.md), [messages](events.yaml) |
| **UC08 Negotiate resource allocation** | Request agent proposals, filter/rank feasible offers, handle timeouts/conflicts and commit compatible resources | [internal](internal.md), [messages](events.yaml) |
| **UC04 + UC08** | One allocation authority, version/capacity checks, idempotency, decisions, outbox and reconciliation | All three contracts |
| UC01 support | Activate agents after C03 registration | `activateAgent` in internal.md |
| UC07 support | Report existing commitments to C05 without silently undoing them | `reconcileAllocations` in internal.md |

HTTP operations use the owner tag `C08 Allocation and Resource Agents`.
Summaries and `x-use-cases` identify UC04/UC08. UC08 has no extra browser HTTP
endpoint: C05 calls `negotiateAllocation` inside the backend and agents exchange
messages. The Redoc build renders HTTP only. Event contracts and cross-component
direct calls remain in their respective source files, following the shared template.

## Complete report operation inventory

The report lists 14 logical operations; this table maps each to its appropriate
boundary. Only HTTP operations, bus messages and cross-component calls are
contracts. Component-local algorithms and transaction helpers stay implementation
details; they do not receive public routes or provided-interface definitions.

| Report operation | Use case | Boundary / eventual implementation  Guaranteed normal output |
| --- | --- | ---  --- |
| `activateAgent(resourceId, profileVersion, key)` | UC01 supporting UC04/UC08 | C03 → C08 direct call; also check trusted service context  `Result<AgentActivation>` |
| `assignVehicle(context, planId, deliveryId, expectedVersion, key)` | **UC04** | C01 HTTP adapter or C05 → C08 direct call  `Result<AssignmentOutcome>` |
| `rankCandidates(requirements, candidates, priorities)` | **UC04 + UC08** | C08-only hard-constraint filtering, scoring and deterministic tie-break  `RankedCandidates` |
| `reserveCandidate(candidateId, expectedResourceVersion, key)` | **UC04** | C08-only expiring reservation  `Result<Reservation>` |
| `recordOfferResponse(offerId, response, key)` | **UC04** | C01 authenticated driver command  `Result<OfferStatus>` |
| `releaseReservation(reservationId, reason)` | **UC04** | C08-only release of uncommitted capacity  `Result<ReservationStatus>` |
| `commitAssignment(reservationId, expectedPlanVersion, key)` | **UC04** | C08-only atomic finalisation of the existing `assignmentRef`  `Result<AssignmentOutcome>` |
| `negotiateAllocation(context, allocationProblem, key)` | **UC08**, callable from UC04/UC07 | C05 → C08 direct call, also used inside C08  `Result<NegotiationOutcome>` |
| `requestProposals(negotiationId, needs, deadline, inputVersions)` | **UC08** | Authority → agents: `ProposalRequested`  `NegotiationTicket` |
| `evaluateProposalRequest(request, localState)` | **UC08** | Agent-local computation; no public contract  `Proposal` |
| `submitProposal(negotiationId, agentId, proposal)` | **UC08** | Agent → authority: `ProposalSubmitted`  `Result<ProposalReceipt>` |
| `commitNegotiation(negotiationId, selectedProposal, expectedVersions, key)` | **UC08** | C08-only atomic commit of the complete feasible selection  `Result<NegotiationOutcome>` |
| `acknowledgeInstruction(instructionId, status, key)` | **UC08**, shared dispatch | Agent → authority: `InstructionAcknowledged`  `Result<InstructionReceipt>` |
| `reconcileAllocations(context, planId, attemptId)` | **UC04 + UC08**, UC07 support | C05 → C08 direct read; preserve valid commitments  `Result<AllocationReconciliation>` |

`getAssignment` is a proposed support query, not a named report API: C01 needs
offer details and authoritative state after a lost response. It makes no
allocation decision. Concrete message names are proposed transport mappings.

Logical `recordOfferResponse` returns `OfferStatus`; the existing HTTP adapter
returns the richer assignment workflow result after optional finalisation.
`AssignmentOutcome` is the report name for the existing `AllocationOutcome`
record. Its fields and the OfferStatus fields are defined in internal.md.

## UC04 invariants

1. Read C05's latest confirmed plan and C03's eligible profiles.
   `expectedVersion` means **plan version**, never resource/delivery version.
2. Reservations expire. Do not hold a transaction/lock while waiting remotely.
3. `awaiting_acceptance` identifies an offer with fixed recipient, resource,
   plan/resource versions, assignment reference and expiry.
4. Only the authenticated recipient can respond. Repeats are harmless; late or
   changed answers cannot revive/overwrite an offer.
5. Acceptance is not commitment. At final commit, recheck acceptance, expiry,
   current versions, eligibility, windows, capacity and mandatory policies.
6. Commit allocation, affected plan/resource updates, C10 decision and outbox
   atomically. Only then return `assigned`, and end candidate search.
7. Reuse an existing compatible UC08 allocation/reference without consuming
   capacity twice. AF1–AF4 must expose an actual unresolved/unassigned/conflict
   result, rather than a success based only on a reservation or acceptance.

## UC08 invariants

1. Use a current authorised plan. Agent scores/resource snapshots are hints;
   C08 revalidates authoritative facts at commitment.
2. Persist negotiation ID, round, versions and one overall deadline. Reject
   stale, expired, wrong-round and unauthorised proposals.
3. **AF1:** an incomplete set of resource needs is not success. Return
   `allocation_failed`/`unresolved` with reasons when no complete solution exists.
4. **AF2:** enforce hard constraints first; then use documented priorities,
   emissions/fairness/workload criteria and stable ID ordering. Record the
   policy/version, rather than trusting agent scores alone.
5. **AF3:** continue with timely agents only if a complete feasible selection
   exists. Record absent agents and stop at the fixed deadline.
6. **AF4:** discard invalidated proposals, refresh versions and retry only
   within the caller's bounded round/deadline budget.
7. Commit the complete selection atomically. A new driver-operated vehicle
   requires driver acceptance **before commitment**, following the report's
   conservative rule. Agent acceptance/receipt is not driver acceptance.
8. Missing instruction receipts do not undo committed allocations. Preserve
   valid commitments on later replanning failure. C07/C05 decide replanning;
   C08 does not automatically cancel and recursively restart itself.

## Pending cross-component agreements

| Owner | Agreement needed before implementation / real integration |
| --- | --- |
| C01/C02 | Role names, driver identity, provider/run authorisation and view updates. No identity-provider URL or role strings are invented here. |
| C03 | Direct activation versus `AgentActivationRequested`; use one command identity if both exist. Resource version and observed availability versus C08-owned capacity. Its current event scope is `development` with `integration_mode=mock`, not the shared live/simulation envelope; agree an authorised adapter before consuming it as operational input. |
| C04/C05 | `deliveryId` versus C04 request ID; confirmed plan schema, time windows and requirement versions; plan/resource updates in the shared transaction. C04 progress events are not yet agreed. |
| C05 | Producer-owned `PlanReady` does not exist in this branch. C08 must add `action: receive` with an external producer `$ref` when published, rather than copying a C05 message definition. |
| C03/C05 | Capacity precision/dimensions and multi-resource selection. Current models have one `capacity {amount, unit}` with `kg/parcels/slots`; simultaneous weight/volume must not be silently dropped. |
| C07 | Keep existing `OperationalDisruption` v1 compatible. Stable `failureId` prevents duplicate replanning; a new material failure gets a new identity. |
| C09/C10 | Evidence/version references, mandatory policy handling and immutable decision evidence in the same commitment transaction. |
| C11/C12 | Review `AllocationCommitted` and provider/run isolation before subscribing. |
| Infrastructure | Proposed exchange `ulop.events`, routing key = event type, per-consumer durable queues, TLS and authority/agent publisher ACLs. The shared envelope lacks a provider field; trusted routing/access checks are required before multi-provider use. |

No numeric timeout, reservation TTL, retry count or score weights are fixed by
the report. Pair configuration must supply bounded values; examples are not
production defaults.

## Existing types and gaps

Draft Python types exist in `backend/app/components/c08_allocation/models.py`
and `events.py`; `C08Service` in `interfaces.py` is a protocol, not a service.
Wire JSON is camelCase; Python attributes are snake_case.

The documentation is ahead of the code for offer details in outcomes, status
lookup, trusted execution scope, activation/reconciliation results, round and
instruction messages, and complete negotiation bundle results. Existing offer
response/commit commands omit trusted context; adapters must carry it in a later
implementation. Existing single-proposal commit types cannot yet express every
multi-resource selection. No Python/runtime changes are part of this task.
The existing negotiation protocol's phrase "without UC04 driver acceptance"
describes a separate agent-proposal stage; it must not be used to justify
committing a new driver-operated vehicle before the required acceptance.

Legacy `plan-ready-event.json` and `resource-availability-changed-event.json`
are retained unchanged because backend tests load them. They are receiver
fixtures, not C08-owned producer definitions. Move them to C05/C03 only with a
coordinated test/ownership update. New examples are C08-provided operations or
C08-produced messages.

The new success examples describe confirmed plan version 3 becoming version 4,
with resource version 7 becoming 8. UC04 and UC08 examples illustrate alternative
entry paths to the same commitment, not two allocations. The legacy operational
failure at plan version 3 is a separate scenario branch, not a later event in
that success chain. All records are synthetic and must not be imported as live data.

## Examples and checks

- UC04: [request](examples/assign-vehicle-request.json),
  [pending offer](examples/assignment-awaiting-acceptance.json),
  [driver answer](examples/offer-response-request.json),
  [assigned](examples/assignment-assigned.json),
  [status](examples/assignment-status.json).
- UC08: [request](examples/negotiate-allocation-request.json),
  [confirmed](examples/negotiation-confirmed.json),
  [reconciliation](examples/allocation-reconciliation.json).
- UC01 support: [activation](examples/agent-activation.json).
- C08 messages have inline synthetic examples in events.yaml. Existing
  [operational failure](examples/operational-disruption-event.json) remains a
  C07 compatibility fixture.

Run `npm run contracts:lint` and `npm run contracts:build`. AsyncAPI is source-only
in current CI. Do not commit generated aggregate/`dist/`. If an existing fixture
changes, also run `cd backend && uv run pytest`.

Local verification on 18 November 2026: OpenAPI lint and Redoc build passed.
The C08-only lint has the repository's missing-license warning; aggregate lint
also reports existing C03 example warnings (12 warnings in total, no errors).
AsyncAPI parser 3.6.6 resolved C08 and the existing C07 consumer with no errors;
its newer-spec advisory is expected because this repository requires 3.0.0.
JSON Schema checks passed for 32 valid samples and rejected 35 invalid variants;
all Markdown links resolve. The existing three fixtures and C07 failure payload
are unchanged, and the existing Python C08/C07 models accept the legacy fixtures.
These are contract checks, not a running API/broker/database integration test.
