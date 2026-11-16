# C08 shared contracts — implementation proposal

Scope: UC04 Assign Vehicle + UC08 Negotiate Resource Allocation.
Pair: Shixian Cai and Óscar Gómez González. Architecture baseline: Group 10,
5 November 2026. This is **not yet a cross-pair-approved contract**.

## Contents

- `contracts/c08/models.py`: strict Pydantic v2 data models.
- `contracts/c08/interfaces.py`: typed C08 logical API protocol (snake_case Python
  equivalents of the architecture's camelCase names).
- `tests/test_c08_contracts.py`: fast, dependency-isolated validation tests.

## Run

From repository root (Python 3.12+):

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

## Invariants

- C05 persists valid planning versions; C08 commits capacity separately.
- Agent receipt is separate from UC04 driver acceptance.
- UC04 finalises an existing `assignment_ref`, never allocates twice.
- Mutating commands use an idempotency key; version checks belong inside short
  PostgreSQL transactions. Pydantic validation alone **cannot** prevent races.
- Failed commitment produces a durable `OperationalDisruption` outbox entry;
  C07 decides whether C05 replans. No C08 restart loop.
- C10 receives actual decision-time evidence content, not a mutable C09 key.

## Cross-pair review before merging

1. C05 (Ye + Zecheng): `PlanReadyPayload` and version meaning.
2. C03 (resource owners): identifiers, resource status, capacity units and
   `resource_version`. `committed_capacity` is C08-owned, even if exposed
   alongside C03 profile data in a composed snapshot.
3. C07 (Bruno + Alejandro): failure taxonomy, `OperationalDisruption` and
   deduplication keys.
4. C10 (decision records): immutable evidence snapshot schema and decision IDs.
5. C01/UC04 driver UI: offer IDs, expiry and driver response statuses.

## Explicitly deferred

- Authenticated broker consumers, persistent outbox and publisher.
- Atomic SQL reservation/commit logic; exclusion constraints; uniqueness rules.
- Negotiation scheduling, agent-specific scoring, retries of notifications.
- Full API/FastAPI route integration or production deployment.

## Important limitations

`EventEnvelope.payload` is an untyped dictionary to avoid pretending that all
producer/consumer event agreements have been finalised; validate the payload
against the selected typed event body after checking `event_type`.

Resource capacity is deliberately represented with numeric units. Confirm
whether the team prefers decimal exactness before using it for monetary or
fractional physical capacity transactions. The sample unit choices and enums
are proposals, not signed-off shared vocabulary.
