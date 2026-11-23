# C08 Allocation and Resource Agents

Contract draft for UC04 and UC08. Óscar Gómez González and Shixian Cai own
the C08 implementation. Python models and the C08 service protocol live in
`backend/app/components/c08_allocation/`.

## HTTP boundary

The [UC04 OpenAPI contract](openapi.yaml) defines the operator's assignment
request and the driver's offer response. The HTTP adapter derives identity from
authentication and passes the idempotency key to the service. Assignment
finalisation and UC08 negotiation are internal C08 operations.

## Event boundaries

- C05 emits [PlanReady](examples/plan-ready-event.json) after operator
  confirmation in UC03 and after the revised plan commits in UC07. The
  version and its outbox intent are saved together. Each event includes
  the full set of resource requirements for unfinished deliveries in that
  version. The single affected plan and its entity version must agree with
  the payload. C05 owns the producer contract.
- The C05 [confirmed simulation v3](../c05-planning/examples/confirmed-plan.json)
  and [route-only revision event v4](../c05-planning/examples/plan-ready-revision-event.json) share a
  plan and run ID. The route and schedule are read from the saved C05 version
  for that run. The requirements stay the same; C08 reuses commitments that
  still fit the revised route and schedule and checks that the plan version
  is current before committing resources.
  For the single-vehicle slice C08 verifies the confirmed revision, driver,
  resource need, eligibility, remaining capacity, route distance and operating
  hours. A compatible revision preserves the offer and any driver acceptance.
  If C08 cannot verify the commitment (including restrictions without matching
  route evidence), it records `unresolved` and releases capacity in the same
  transaction. The operator can see the unresolved assignment in C01. Selection
  of a replacement and notification of C07 are pending cross-component work.
- `ResourceAvailabilityChanged` uses [C03's producer contract](../c03-resource-management/events.yaml).
  UC04 checks whether the change affects the vehicle selected for a pending
  assignment. See the [C03 availability example](examples/resource-availability-changed-event.json).
  The C03 producer currently has a component-specific envelope.
  C03 owns the change to the shared envelope.
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
