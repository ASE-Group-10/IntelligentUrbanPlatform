# C08 Allocation and Resource Agents

Contract draft for UC04 and UC08. Óscar Gómez González and Shixian Cai own
the C08 implementation. Python models and the C08 service protocol live in
`backend/app/components/c08_allocation/`.

## Event boundaries

- [PlanReady example](examples/plan-ready-event.json). C05 emits this after
  saving a plan version and its outbox record. C08 validates the event as
  `PlanReadyEvent`. The payload fields and capacity vocabulary are proposed
  for C05 review. C05 owns the eventual producer definition.
- [OperationalDisruption example](examples/operational-disruption-event.json).
  C08 sends the actual committed state and the unresolved delivery IDs.
  C07 uses `failureId` as the stable trigger identity. Broker redelivery
  reuses `eventId`; it does not start a second negotiation or replan.

## C08 provides

| Operation | Input | Result and lifecycle |
| --- | --- | --- |
| `handle_plan_ready` | `PlanReadyEvent` | `AllocationOutcome`. Deduplicate the event, skip superseded versions, and reuse valid commitments. |
| `negotiate_allocation` | `AuthContext`, `AllocationProblem`, command key | `AllocationOutcome`. Run one bounded proposal round. UC08 agent receipts are independent of driver acceptance. |
| `commit_negotiation` | `CommitNegotiationCommand` with proposal and expected plan/resource versions | `AllocationOutcome`. Commit capacity and report the selected resource, assignment reference and resulting versions. |
| `assign_vehicle` | `AssignVehicleCommand` with plan, delivery, expected version and key | `AllocationOutcome`. Reuse the UC08 reference when available, or reserve a candidate for an offer. |
| `record_offer_response` | `OfferResponseCommand` with offer, response and key | `AllocationOutcome`. Record the driver's acceptance or rejection in UC04. |
| `commit_assignment` | `CommitAssignmentCommand` with assignment reference, expected plan version and key | `AllocationOutcome`. Recheck versions, eligibility and capacity, then finalise the accepted reference without consuming capacity again. |
| `publish_operational_disruption` | `AuthContext`, `OperationalDisruptionPayload`, command key | Event UUID. Persist the failure and outbox intent together. C07 evaluates any separate replan. |

Mutations replay the prior result for the same command and key. A reused key
with a different command payload returns a conflict. Allocation commands can
return conflict, forbidden, invalid or unavailable outcomes, plus the relevant
workflow status defined by `OutcomeCode`. Commit authoritative state, C10
decision evidence and outbox intent in one short PostgreSQL transaction.
Publish after commit. Executable driver instructions require accepted and
committed UC04 assignment state.

## Required interfaces and review points

- C05 supplies the saved plan version and resource needs. Review the
  `PlanReady` fixture and the authoritative plan-version lookup at commit.
- C03 owns resource profiles, observed availability and resource versions.
  C08 owns reserved and committed capacity. Agree the capacity dimensions,
  numeric precision, and resource-version checks before writing migrations.
- C10 supplies the decision/evidence write contract. Preserve the exact
  decision-time content and source versions in the authoritative transaction.
- C01 and C08 agree driver identity, offers, expiry and responses. Confirm
  how the assignment reference binds the resource version used at finalisation.
