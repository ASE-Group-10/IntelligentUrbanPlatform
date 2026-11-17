# UC01 implementation verification — 16 November 2026

Material design decisions assisted by AI and subject to pair review:

- Match the live resource table: timezone, operating hours, and restrictions remain
  inside `details` JSONB. No pending-review state or endpoint.
- Use discriminated Pydantic create schemas and revalidate complete update results.
- Preserve the requested POST update and `/agent-activation` routes.
- Share resource mutation, durable receipt, audit stand-in, and development outbox
  in a short SQLAlchemy transaction; serialize matching idempotency scopes.
- Use a dedicated restricted backend login and provider-scoped row-level policies.
- Deliver a small static test harness while the shared React product UI is pending.

Verification completed:

- 17 isolated API tests passed against SQLite; all four resource types exercised.
- Python lint/format and JavaScript syntax checks passed.
- Actual PostgreSQL connection established through the configured database URL.
- Two concurrent live activation commands with the same key returned one original
  and one replayed response; the already-active agent and resource version stayed unchanged.
- The dbmate migration has reversible up/down sections; run the documented Docker
  commands to verify it against a local PostgreSQL database.
- Browser: registered `DEMO-UC01-VAN`, updated its name, observed version 2, and
  replayed the update with the same key without another resource/version change.
- Browser: registered `DEMO-UC01-RETRY` with failed activation and saw it remain
  ineligible; retried activation successfully and observed version 2 and eligibility.
- Both synthetic demo records remain available for manual testing.

Limitations:

- C02, mapping, and C08 are deterministic mocks; no external provider was exercised.
- C10 is a C03-local successful-command audit stand-in. Failed/denied requests have
  typed errors/correlation IDs; complete durable failure/access auditing awaits C10.
- Outbox records are unpublished and explicitly development scoped. There is no
  broker worker, live resource-agent execution, allocation, or end-to-end planner.
- Operating hours and restrictions are recorded; full planning feasibility is C05.
- No UC02 implementation, production authentication, Docker deployment, or cloud
  deployment has been delivered in this UC01 increment.
- Schema/application changes need pair review and normal integration into `develop`.
