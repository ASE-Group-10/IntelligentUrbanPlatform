"""C05 version history and generation receipts in PostgreSQL."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.shared.contracts import RunScope, RunScopeSelector
from app.shared.result import Failure


class GenerationConflictError(Exception):
    """The caller reused a generation key with different input."""


class DecisionRejectedError(Exception):
    """Exit the caller's transaction on a C10 failure, rolling back its writes."""

    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.message)


def scope_key(scope: RunScope, run_id: UUID | str | None) -> str:
    if scope == "simulation" and run_id is not None:
        return f"simulation:{UUID(str(run_id))}"
    if scope == "live" and run_id is None:
        return "live"
    raise ValueError("simulation requires a run ID and live must omit it")


def plan_lock_key(scope: RunScopeSelector, plan_id: UUID) -> str:
    return json.dumps(["C05.plan", scope_key(scope.scope, scope.run_id), str(plan_id)])


def lock_plan(conn: psycopg.Connection, scope: RunScopeSelector, plan_id: UUID) -> None:
    """Serialize version checks and appends with C08's commit-time read lock."""
    conn.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (plan_lock_key(scope, plan_id),),
    )


class PostgresPlanStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """Commit C05 and C10 on normal exit, or roll both back on exception."""
        with psycopg.connect(self._conninfo, connect_timeout=5) as conn:
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            yield conn

    def replay(
        self,
        conn: psycopg.Connection,
        scope: RunScope,
        run_id: UUID | None,
        principal_id: str,
        key: str,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], UUID] | None:
        """Return the original proposal and its delivery trace under the key lock."""
        scoped = scope_key(scope, run_id)
        # Lock before querying C09 or writing a decision. Under READ COMMITTED
        # the next statement sees the first caller's receipt after waiting.
        lock_name = json.dumps(["C05.generatePlan", scoped, principal_id, key])
        conn.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_name,)
        )
        row = conn.execute(
            """select r.request_body = %s, v.proposal, v.correlation_id
                 from c05_planning.generation_receipts r
                 join c05_planning.plan_versions v using (scope_key, plan_id, version)
                where r.scope_key = %s and r.principal_id = %s
                  and r.idempotency_key = %s""",
            (Jsonb(request), scoped, principal_id, key),
        ).fetchone()
        if row is None:
            return None
        if not row[0]:
            raise GenerationConflictError(
                "The generation key was used for another request."
            )
        return row[1], row[2]

    def save(
        self,
        conn: psycopg.Connection,
        proposal: dict[str, Any],
        correlation_id: UUID,
        planned_departure_at: datetime,
        principal_id: str,
        key: str,
        request: dict[str, Any],
    ) -> None:
        """Save a version and receipt on the connection used for its C10 decision."""
        run_scope = proposal["runScope"]
        scoped = scope_key(run_scope["scope"], run_scope.get("runId"))
        if planned_departure_at.utcoffset() is None:
            raise ValueError("The planned departure must include a UTC offset.")
        conn.execute(
            """insert into c05_planning.plan_versions
                      (proposal, correlation_id, planned_departure_at)
               values (%s, %s, %s)""",
            (Jsonb(proposal), correlation_id, planned_departure_at),
        )
        conn.execute(
            """insert into c05_planning.generation_receipts
                      (scope_key, principal_id, idempotency_key, request_body,
                       plan_id, version)
               values (%s, %s, %s, %s, %s, %s)""",
            (
                scoped,
                principal_id,
                key,
                Jsonb(request),
                proposal["planId"],
                proposal["version"],
            ),
        )

    def get_plan(
        self,
        scope: RunScope,
        run_id: UUID | None,
        plan_id: UUID,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        """Read a committed snapshot from this scope, by version or latest first."""
        scoped = scope_key(scope, run_id)
        with self.transaction() as conn:
            row = conn.execute(
                """select proposal from c05_planning.plan_versions
                    where scope_key = %s and plan_id = %s
                      and (%s::integer is null or version = %s)
                    order by version desc limit 1""",
                (scoped, plan_id, version, version),
            ).fetchone()
        return row[0] if row is not None else None
