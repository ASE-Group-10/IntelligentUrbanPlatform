# Intelligent Urban Logistics Optimisation Platform

CS7CS3 Advanced Software Engineering — Group 10.

A platform for coordinating urban deliveries using resource agents, city conditions,
planning, policy knowledge, and explainable decisions.

This README defines the thin-slice scope and collaboration baseline. Implementation,
deployment, and automated checks must be verified in the application repository;
these documents do not indicate that those facilities already exist.

## Thin-slice goal

Deliver one repeatable scenario across all twelve architectural components:

1. Authenticate a user, register resources, and submit delivery requests.
2. Read city conditions and retrieve applicable logistics policy evidence.
3. Generate a feasible plan and present its explanation.
4. Obtain operator confirmation, publish `PlanReady`, and process resource allocation.
5. Obtain driver acceptance before executable dispatch where required.
6. Inject a road closure, detect its impact, and save a revised plan.
7. Display basic labelled KPIs and replay the recorded run without live side effects.

Use a small Dublin scenario: two deliveries, a van, a cargo bike, a depot, and a
small curated knowledge corpus. Start with simple feasible algorithms. Label
simulation inputs and test substitutes explicitly.

The planning milestone is **27 November 2026**, subject to course announcements.

## Architecture and responsibilities

| Component | Responsibility |
| --- | --- |
| C01 — Web Interface | Forms, map, confirmation, driver response, explanations, KPIs, and replay controls |
| C02 — Identity and Access | Authentication and role/scope authorisation |
| C03 — Resource Management | Resource profiles, capabilities, observed availability, and eligibility |
| C04 — Delivery Management | Validated delivery requests, constraints, and progress |
| C05 — Planning and Replanning | Feasible proposals, confirmed plans, revisions, and plan versions |
| C06 — City Monitoring | Observations, current City State, history, and data quality |
| C07 — Disruption Detection | Impact assessment and relevant replanning triggers |
| C08 — Allocation and Resource Agents | Agent proposals, commitments, UC04 acceptance, and execution status |
| C09 — Logistics Knowledge | Trusted current corpus, retrieval, applicability, and provenance |
| C10 — Decision Records and Explanation | Immutable decision-time evidence and grounded explanations |
| C11 — KPI Analytics | Read-only indicators with units, coverage, timestamps, and estimate labels |
| C12 — Simulation and Replay | Reproducible runs, recording, and isolated playback |

Important boundaries:

- C05 saves plans; C08 commits resources. A saved plan does not imply allocation.
- Driver acceptance belongs to UC04. Agent acknowledgement is a separate state.
- C06 owns City State. C07 evaluates disruptions and requests separate replanning.
- C10 preserves the evidence used at decision time. Historical explanations do not
  retrieve replacement evidence from today's C09 corpus.
- Replay cannot write live state or publish live commands.
- Related authoritative state, decision evidence, and outbox entries commit
  atomically. Consumers handle duplicate delivery and entity-version checks.

## Proposed working pairs

These pairings build on the previous use-case assignments. Confirm them at kickoff.
They coordinate work; code remains collectively owned.


| Pair / developer | Assigned scope | Components |
|---|---|---|
| **Julian + Jolin** | UC01 Register logistics resource; UC02 Submit delivery request | C03, C04 |
| **Ye + Zecheng** | UC03 Generate delivery plan; UC07 Replan deliveries | C05 |
| **Shixian + Oscar** | UC04 Assign vehicle; UC08 Negotiate resource allocation | C08 |
| **Bruno + Alejandro** | UC05 Monitor city conditions; UC06 Detect disruption | C06, C07 |
| **Qingyuan + Bangchen** | UC09 Query logistics knowledge; UC10 Explain logistics decision | C09, C10 |
| **Jolin** | UC11 Monitor KPIs; UC12 Replay simulation | C11, C12 |

UC11 and UC12 remain assigned to Jolin until the team agrees a handover. The proposed
support split is UC11 to Qingyuan/Bangchen and UC12 to Bruno/Alejandro, with Jolin
providing the requirements handover. Record the agreed decision in an issue.

C01 views are delivered alongside each workflow. Assign a named coordinator for
the shared C01 layout and C02 authentication during bootstrap.

## Technical baseline

- Frontend: React and TypeScript.
- Backend: a modular Python/FastAPI core with separate workers where needed.
- Messaging: RabbitMQ and versioned JSON events.
- Persistence: Supabase-managed PostgreSQL; PostGIS and pgvector where required.
- Local integration: Docker Compose.
- Deployment: select AWS or Azure; frontend hosting remains a separate choice.

The twelve components are logical boundaries, not a requirement for twelve
independently deployed services.

## Getting started

1. Clone the team repository and check out `develop`.
2. Read [CONTRIBUTION.md](CONTRIBUTION.md) before making changes.
3. Select a small issue and agree affected contracts with the consuming pair.
4. Create a feature branch from the latest `develop`.
5. Implement with your partner, test, and open a pull request into `develop`.

Replace `<REPOSITORY_URL>` with the real team repository URL:

```sh
git clone <REPOSITORY_URL> urban-logistics-platform
cd urban-logistics-platform
git switch develop
git pull --ff-only origin develop
git switch -c feature/1-resource-registration
```

The repository administrator must create `develop` before this workflow is used.
See the one-time bootstrap procedure in [CONTRIBUTION.md](CONTRIBUTION.md).

### Application bootstrap checklist

The first implementation issue must add and verify:

- Supported Python and Node versions and dependency lockfiles.
- Backend and frontend dependency-installation commands.
- `.env.example` with non-secret variable names and local configuration guidance.
- Docker Compose services and documented startup/shutdown commands.
- Versioned migrations and a repeatable synthetic seed dataset.
- Exact commands for tests, formatting, linting, type checks, and builds.
- Automated PR checks using those same commands.

Add executable setup commands here when they have been verified from a clean clone.
Keep local secrets out of Git; privileged database keys never belong in the browser.

## Suggested repository structure

Create this structure as implementation begins; it is not an inventory of existing files.

```text
.
├── README.md
├── CONTRIBUTION.md
├── frontend/              # C01 presentation
├── backend/
│   ├── app/
│   │   ├── components/    # C02–C12 modules behind explicit interfaces
│   │   └── infrastructure/ # Auth, persistence, broker, and connector adapters
│   └── tests/
├── contracts/             # Versioned API/event schemas and example payloads
├── database/migrations/
├── fixtures/              # Synthetic scenario data and recorded test inputs
├── docs/                  # Setup, decisions, architecture, and demo instructions
└── .github/workflows/     # Automated checks when implemented
```

## Integration order

1. Resources and requests → initial planner.
2. Knowledge and saved evidence → planning and explanation.
3. Confirmed plan → `PlanReady` → allocation → UC04 driver acceptance.
4. City change → disruption detection → saved revision.
5. Operational records → KPIs and isolated replay.

Agree shared fields and examples first. Pairs may develop against those examples,
but the demonstration must exercise actual integrated components.

## Git Flow

`develop` holds integrated work. `main` holds tagged, verified releases.
Short feature branches merge into `develop`; release and hotfix branches follow
the procedures in [CONTRIBUTION.md](CONTRIBUTION.md).

## Thin-slice completion

- The demonstration runs from a documented, clean setup.
- Every component participates in executable behaviour.
- Authorisation, capacity integrity, idempotency, and version conflicts are checked.
- Plans, allocations, driver acceptance, and receipts are displayed accurately.
- Decision evidence survives knowledge-corpus updates.
- Replay has no live side effects.
- Test results, incomplete integrations, and limitations are documented.
