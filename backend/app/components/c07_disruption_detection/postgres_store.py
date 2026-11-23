"""PostgreSQL ``DisruptionStore`` (schema ``c07_disruption_detection``).

Tables come from ``database/migrations/20261122200000_create_c07_disruption_tables``.

A transaction first takes an advisory lock on its scope, so two workers
evaluating events of the same scope cannot both see an event as unprocessed.
The processed-event mark and the saved disruption commit together or not at all.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg import IsolationLevel
from psycopg.types.json import Jsonb

from app.components.c07_disruption_detection.contracts import Disruption
from app.components.c07_disruption_detection.store import (
    DisruptionStoreUnavailableError,
)
from app.shared.contracts import RunScope


def scope_key(scope: RunScope, run_id: UUID | None) -> str:
    """``live`` or ``simulation:<runId>``, the same keys C06 uses."""
    return "live" if scope == "live" else f"simulation:{run_id}"


class PostgresDisruptionTransaction:
    """``DisruptionTransaction`` inside one open database transaction."""

    def __init__(self, conn: psycopg.Connection, key: str) -> None:
        self._conn = conn
        self._key = key

    def is_processed(self, event_id: UUID) -> bool:
        row = self._conn.execute(
            "select 1 from c07_disruption_detection.processed_events"
            " where scope_key = %s and event_id = %s",
            (self._key, event_id),
        ).fetchone()
        return row is not None

    def mark_processed(self, event_id: UUID) -> None:
        self._conn.execute(
            "insert into c07_disruption_detection.processed_events"
            " (scope_key, event_id) values (%s, %s) on conflict do nothing",
            (self._key, event_id),
        )

    def save_disruption(self, disruption: Disruption) -> None:
        self._conn.execute(
            "insert into c07_disruption_detection.disruptions"
            " (scope_key, disruption_id, version, disruption, created_at, updated_at)"
            " values (%s, %s, %s, %s, %s, %s)"
            " on conflict (scope_key, disruption_id) do update set"
            " version = excluded.version, disruption = excluded.disruption,"
            " updated_at = excluded.updated_at",
            (
                self._key,
                disruption.disruption_id,
                disruption.version,
                Jsonb(disruption.model_dump(mode="json", by_alias=True)),
                disruption.created_at,
                disruption.updated_at,
            ),
        )

    def disruption_for_event(self, event_id: UUID) -> Disruption | None:
        row = self._conn.execute(
            "select disruption from c07_disruption_detection.disruptions"
            " where scope_key = %s and disruption -> 'triggerEventIds' ? %s",
            (self._key, str(event_id)),
        ).fetchone()
        return Disruption.model_validate(row[0]) if row else None


class PostgresDisruptionStore:
    """``DisruptionStore`` backed by PostgreSQL (local Docker or Supabase).

    Args:
        conninfo: libpq connection string, e.g. the ``DATABASE_URL`` from ``.env``.
    """

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(
        self, scope: RunScope, run_id: UUID | None
    ) -> Iterator[PostgresDisruptionTransaction]:
        key = scope_key(scope, run_id)
        with self._connect() as conn:
            try:
                with conn.transaction():
                    conn.execute(
                        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"c07:{key}",),
                    )
                    yield PostgresDisruptionTransaction(conn, key)
            except psycopg.OperationalError as error:
                raise DisruptionStoreUnavailableError(
                    f"Disruption store failed: {error}"
                ) from error

    def disruptions(self, scope: RunScope, run_id: UUID | None) -> list[Disruption]:
        """The disruptions saved in one scope, oldest first."""
        with self._connect() as conn:
            conn.read_only = True
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            try:
                rows = conn.execute(
                    "select disruption from c07_disruption_detection.disruptions"
                    " where scope_key = %s order by created_at, disruption_id",
                    (scope_key(scope, run_id),),
                ).fetchall()
            except psycopg.OperationalError as error:
                raise DisruptionStoreUnavailableError(
                    f"Disruption store failed: {error}"
                ) from error
        return [Disruption.model_validate(row[0]) for row in rows]

    def processed(self, scope: RunScope, run_id: UUID | None) -> set[UUID]:
        """The ids of the events marked as processed in one scope."""
        with self._connect() as conn:
            rows = conn.execute(
                "select event_id from c07_disruption_detection.processed_events"
                " where scope_key = %s",
                (scope_key(scope, run_id),),
            ).fetchall()
        return {row[0] for row in rows}

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        try:
            connection = psycopg.connect(self._conninfo, connect_timeout=5)
        except psycopg.OperationalError as error:
            raise DisruptionStoreUnavailableError(
                f"Disruption store unreachable: {error}"
            ) from error
        with connection as conn:
            yield conn
