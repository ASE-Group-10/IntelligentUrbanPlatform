# Contribution Guide

Follow this guide for paired development, shared models, pull requests, and releases.
Project scope and component responsibilities are in [README.md](README.md).

## 1. Git Flow branch rules

We use Git Flow with `main` as the release branch. A Git Flow extension is not
required; normal Git commands and GitHub pull requests are sufficient.

| Branch | Start from | Merge into | Purpose |
| --- | --- | --- | --- |
| `main` | Initial repository commit | — | Tagged, verified releases |
| `develop` | Initial `main` | Through a release branch | Integrated work for the next release |
| `feature/<issue>-<description>` | Latest `develop` | `develop` | One small feature, contract, refactor, documentation change, or pre-release fix |
| `release/<version>` | `develop` | `main` and `develop` | Stabilisation, release fixes, and release documentation |
| `hotfix/<version>-<description>` | Latest `main` | `main` and `develop`; also an active release branch | Urgent correction to an already released version |

Examples: `feature/12-resource-registration`, `feature/18-plan-contract`,
`release/0.1.0`, and `hotfix/0.1.1-allocation-check`.

- Use lowercase, hyphen-separated descriptions and the actual issue number.
- Create branches per story/change, not permanent branches per person or component.
- Keep changes small enough to integrate within one or two working days where possible.
- Do not push directly to `main` or `develop` after initial bootstrap.
- Use GitHub **Create a merge commit** for PRs, including release/hotfix merges.
  Do not squash or rebase release/hotfix merges between long-lived branches.
- Do not rewrite published shared-branch history or force-push over a partner's work.

## 2. One-time repository bootstrap

For a new, empty repository only, the administrator places these two files in the
repository root, creates the initial `main` commit, adds the actual `origin` remote,
and pushes `main`. Then create the integration branch:

```sh
git switch main
git switch -c develop
git push -u origin develop
```

If the repository already exists, preserve its history. Add these documents through
a normal feature PR; create `develop` only if it is missing.

Configure the GitHub repository:

- Invite the team and make `develop` the default development/PR base branch.
- Enable merge commits.
- Where supported by repository settings, protect `main` and `develop`: require PRs,
  at least one approval, resolved discussions, and no force pushes or deletion.
- Include administrators in the protection policy where available.
- Do not require linear history; this workflow uses merge commits.
- Add required status checks after their CI jobs exist and have run successfully.
  Do not select checks that have not been implemented.
- Keep deployment secrets in the configured secret store, never in commits.

Reviewers must be outside the implementing pair. The pair enforces this even if
GitHub's approval settings cannot express that distinction.

## 3. Start a small issue

Before coding, record:

- The user-visible outcome and UC/component references.
- Both pair members and the expected reviewer/consuming pair.
- Acceptance examples, including relevant failure cases.
- Dependencies and any shared schema or migration changes.
- Whether data or external integrations are simulated.

Select one active story per pair. Technical coordination ownership does not prevent
another pair from improving a component through the same review process.

### Choosing an issue template

When opening a new issue, choose the template that matches the work. Each issue
should describe a small task, its purpose, and how completion will be checked.

1. **[User story](.github/ISSUE_TEMPLATE/01_user_story.yml) - add a capability for a user.**
   Use this when someone needs to do something new or receive useful information
   from the platform. Focus on the user outcome and observable acceptance examples.

   **Example:** `View disruptions affecting active deliveries`. The Logistics
   Operator needs to see which disruptions affect their active deliveries. Describe
   the expected behaviour and examples that demonstrate it works.

2. **[Technical task](.github/ISSUE_TEMPLATE/03_technical_task.yml) - complete technical or supporting work.**
   Use this for configuration, API/event/shared-model contracts, database
   migrations, tests, documentation, or refactoring. Define concrete deliverables
   and how they will be verified.

   **Example:** `Define the CityStateUpdated event contract`. C06 and C07 need an
   agreed event structure before implementing their integration. Identify the
   producer and consumer, the schema and examples to deliver, and compatibility
   checks. Obtain agreement from both pairs before implementation. Other examples
   include setting up Docker Compose or adding CI checks.

3. **[Bug report](.github/ISSUE_TEMPLATE/02_bug_report.yml) - record observed incorrect behaviour.**
   Use this when something behaves differently from what is expected. Include
   reproduction steps, expected and actual results, the version/environment, and
   sanitised evidence where available. You do not need to know the solution before
   reporting the bug.

   **Hypothetical example:** `Duplicate disruption records after repeated event delivery`.
   Receiving the same event twice creates two disruption records instead of one.
   Record the starting state and steps that reproduce the problem.

Prepare user stories and technical tasks before implementation. Report bugs when
they are observed. Backlog items can use `To be assigned` for the pair/reviewer;
agree both pair members and an outside reviewer before work starts.

Issues can be related: a user story may depend on a technical task, and testing
that functionality may reveal a bug. Link related issues and dependencies. We do
not need all three templates for every change; choose the one that best describes
the work.

## 4. Agree models and contracts before parallel implementation

Agree shared domain meanings together; agree API/event fields between their producer
and consumer. The owning pair can independently design internal objects and tables.
Database and transport representations do not need identical structures.

For each shared model/API/event, put the following in `contracts/`:

- Named fields, types, required/optional rules, and validation constraints.
- Identifier formats, timestamp conventions, units, and status meanings.
- A realistic valid example and relevant error examples.
- Schema version and producing/consuming components.

Use timezone-aware UTC timestamps; label Dublin-local displays. Make capacity,
distance, duration, money, and emission units explicit. Keep plan status, allocation
status, driver acceptance, and instruction receipt separate.

### Changing a contract

1. Open an issue or draft PR with the proposed schema and examples.
2. Obtain agreement from the affected producer and consumer pairs.
3. Update the schema, fixtures, validation, and consuming code together, or provide
   a documented compatible transition.
4. Add contract/integration checks and merge through `develop`.

Do not silently rename a field, change a unit, or reinterpret a status. Coordinate
shared-file edits and migrations before starting. Do not edit another component's
tables directly; use its interface. Core write modules may share a transaction to
preserve the architecture's atomicity requirements.

Contract-shaped test doubles may unblock independent work. They must be labelled,
kept consistent with examples, and replaced by real collaborators for integration
and demonstration evidence.

## 5. Daily paired development

Start from a clean working tree and current `develop`:

```sh
git switch develop
git pull --ff-only origin develop
git switch -c feature/12-resource-registration
```

Replace the sample branch with your actual issue and change.

- Driver: writes the current test/implementation and explains the reasoning.
- Navigator: checks requirements, context, edge cases, and design.
- Swap roles approximately every 25–30 minutes or after a small TDD cycle.
- Write a failing behavioural test, implement the smallest change, then refactor.
- Record both contributors on the PR; use a Git co-author trailer with the partner's
  actual name/email when appropriate.
- Share a brief daily update: working behaviour, blockers, and interface changes.
- Rotate partners at story boundaries with a short handover. Keep continuity while
  the first thin-slice integration is underway.

Stage only intended files, inspect the staged diff, and commit:

```sh
git add backend/app/components/c03_resources.py
git diff --cached
git commit -m "feat(c03): validate resource capacity"
git push -u origin feature/12-resource-registration
```

The path is illustrative; stage the actual changed implementation and test files.
Use commit prefixes such as `feat`, `fix`, `test`, `docs`, `refactor`, and `chore`.

To incorporate integration changes into the shared feature branch:

```sh
git fetch origin
git merge origin/develop
```

Agree with your partner before merging. Resolve conflicts by checking both intended
behaviours, rerun relevant checks, commit the resolution if needed, and push normally.

## 6. Coding and testing standards

| Area | Standard |
| --- | --- |
| General | UTF-8, LF, final newline, spaces, English identifiers, explicit units |
| Python | PEP 8 naming; four spaces; 88-character formatter target; Ruff formatting/linting |
| Python interfaces | Type annotations and public-interface documentation; configured type checks |
| TypeScript/React | Strict checking; two spaces; camelCase functions; PascalCase types/components; ESLint/Prettier |
| Structure | Cohesive modules; domain logic separate from transport, persistence, and external adapters |
| Persistence | Versioned migrations, parameterised access, constraints, short atomic transactions |
| Reliability | Command idempotency, expected versions, consumer deduplication, bounded observable retries |
| Security | Server-side role/scope checks; injected secrets; no privileged browser credentials |
| Logging | Structured operation/entity/correlation IDs; no secrets or unnecessary personal data |
| Evidence | Exact saved decision-time evidence; no reconstruction from the current knowledge corpus |

Pin dependency/tool versions and store shared configurations in Git. Exact runnable
commands must be added during bootstrap and used both locally and in CI.

Use pytest for Python; select and configure shared frontend/browser testing tools
during bootstrap. Cover normal, boundary, and failure cases. Defect fixes include a
regression test. Use controlled clocks, isolated data, and fixed simulation seeds
where appropriate. Separate deterministic routine checks from real-provider checks.

Explicitly verify authorisation, capacity conflicts, duplicate commands/events,
version conflicts, historical evidence, and replay isolation. Record limitations
and unimplemented integrations rather than reporting fixture results as live success.

AI assistance is permitted. Both partners must review, test, and explain generated
changes. Record material AI-assisted decisions and verification outcomes; never
include secrets in prompts or commit unexamined generated code.

## 7. Pull requests and integration

Push your feature branch and open a PR with **base `develop`**. Open a draft early
when a contract or integration needs discussion. Request review from outside the pair;
include a consuming pair when an interface changes.

Use this description:

```markdown
## Outcome
What user behaviour changes, and why?

## Scope
- Issue and UC/component references:
- Pair members:
- Contract, migration, or configuration changes:

## Verification
- Checks run and results:
- Demonstration steps or evidence:
- Simulation/test substitutes and known limitations:
```

Before merging:

- Acceptance criteria and relevant regression tests pass.
- Both partners understand the implementation.
- One reviewer outside the pair approves; discussions are resolved.
- Affected producer/consumer pairs agree on contract changes.
- Existing automated checks pass against the proposed merge result. Before CI exists,
  record the checks run manually and track CI setup as a bootstrap issue.
- Relevant migrations, fixtures, contracts, and setup documentation are updated.
- No credentials, local environments, build outputs, or real customer data are staged.

Merge using **Create a merge commit**. Delete the remote feature branch after merge.
Pull the updated `develop` before starting the next story. If integrated behaviour
breaks, prioritise a corrective feature PR or reviewed revert.

## 8. Release the thin slice

The release coordinator creates the branch from passing `develop`:

```sh
git switch develop
git pull --ff-only origin develop
git switch -c release/0.1.0
git push -u origin release/0.1.0
```

1. Allow only release fixes, version updates, and release documentation on this branch.
   Review fixes through PRs into the release branch.
2. Verify clean setup, all twelve components, the scenario, and documented limitations.
3. Open a PR from `release/0.1.0` to `main`; review and merge with a merge commit.
4. Tag the resulting verified `main` commit:

   ```sh
   git fetch origin
   git switch main
   git pull --ff-only origin main
   git tag -a v0.1.0 -m "Thin-slice release"
   git push origin v0.1.0
   ```

   Coordinate release merges so `main` still identifies the verified release commit.

5. Open a second PR from the same release branch to `develop` to carry back all
   release changes. Merge it with a merge commit. If already contained, confirm this
   in the release record rather than making an empty PR.
6. Delete the release branch only after both targets contain its changes. Publish
   release notes identifying the tag, demonstration steps, checks, and limitations.

Use new version numbers for later releases. Never move a published release tag.

## 9. Hotfix an existing release

For an urgent defect in a tagged release, create `hotfix/<next-version>-<description>`
from current `main`, add a regression test, and verify the correction. Open reviewed
PRs from that branch to `main` and `develop`. If a release branch is active, merge
the fix there as well so its later release cannot reintroduce the defect.

Use merge commits. Tag the corrected `main` commit with the new version, publish
the correction notes, and delete the hotfix branch after all targets contain it.
Pre-release defects in normal development use feature branches from `develop`.

## References

- [Original Git Flow branching model](https://nvie.com/posts/a-successful-git-branching-model/)
- [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
