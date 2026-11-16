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
- Actual Supabase PostgreSQL connection established through its session pooler.
- Actual restricted login: row-level security denies unscoped reads and isolates
  Provider A from Provider B; physical deletion is not granted; denied API writes
  leave the resource version unchanged.
- Two concurrent live activation commands with the same key returned one original
  and one replayed response; the already-active agent and resource version stayed unchanged.
- Both migrations executed successfully in a temporary PostgreSQL schema and were
  rolled back. Hosted migration history is aligned; push dry-run reports no changes.
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
