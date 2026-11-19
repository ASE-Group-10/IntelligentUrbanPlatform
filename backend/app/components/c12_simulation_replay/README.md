# C12 replay engine increment — #17

`ReplayService` executes start, read-only polling, play/pause/resume/seek/stop and
trusted scheduler advancement using the existing replay value contracts. Each
session belongs to an authenticated principal supplied by the trusted host. An
injected C02 policy checks run access on start and again on every session call;
there is no permissive default. Session IDs are not authorisation credentials.

The private `ArchiveReader` boundary provides a manifest, a checkpoint and full
saved state snapshots after each ordered source outcome. `RecordedStep` and
`SavedRun` are internal adapter values, **not** new producer event/HTTP contracts.
The initial checkpoint content uses `snapshot_sha256`: SHA-256 of UTF-8 canonical
JSON with sorted keys and compact separators, containing state, decisionIds and
recordedKpis. Session/presentation fields are excluded. This adapter format and
digest convention still need recorder-owner agreement before persistent use.

Startup rejects wrong runs, invalid captured frames, checkpoint digest mismatch,
sequence gaps, duplicate event IDs, backward timestamps and missing tail records.
The selected half-open range must fit entirely inside one manifest available
range. Outcomes before the start rebuild initial state; equal timestamps retain
sequence order; outcomes at the exclusive end are not applied. A malformed
archive conservatively returns CannotReplay with no verified available ranges.

Advancement applies exactly one complete recorded snapshot. It uses original
KPIs/decision IDs, without executing a planner or current analytics. Polling never
advances. Seek rebuilds from the checkpoint and recorded sequence; a failed read
leaves the old frame/cursor and pauses. Resume retries the next unread outcome.
Reads that differ from the validated archive also pause. Expected-cursor or
transition conflicts change nothing. Completion and stop are terminal. Returned
frames are defensive copies. A process-local lock serialises operations; bounded
session storage has explicit terminal-session cleanup by the trusted host.

Repeatable demonstration from `backend/`:

```sh
uv run pytest tests/test_c12_replay.py -v
```

The reader deliberately has no live-write, command, planner or publisher methods;
the tests run successfully with just load/read_step. This proves the dependency
boundary, **not** restricted PostgreSQL credentials, broker ACLs or network
isolation. Tests use labelled synthetic source outcomes and a policy double.

Remaining for #17 and the integrated thin slice:

- A real isolated simulation generator/recorder with complete outcomes and
  configuration objects; verify configuration integrity/reproducibility.
- Persistent recording/session stores with agreed retention, keys, schema and
  up/down migrations. Current state is process-local and lost on restart; archive
  validation loads the run into memory, so this is a small-run prototype.
- C02 authenticated context adapter and proof that replay credentials are denied
  live database writes and broker publication.
- C10 run-scoped historical explanations and missing-evidence handling. Current
  replay returns original recorded decision IDs only; no evidence is fabricated.
- Run listing, simulation creation, HTTP routing/idempotency receipts, C01 timeline
  and a pacing scheduler. Speed is validated by the existing request contract;
  this service advances one outcome per trusted tick and does not implement timing.

No shared contract, migration, dependency or other component changes. Keep #17
open. Prepared by Jolin with AI assistance; README pair Jolin + Qingyuan, no
completed paired session or UC12 handover claimed. Outside review: Bruno/C06 or
Oscar/C08. Neither this prototype nor fixture checks are the finished thin slice.
