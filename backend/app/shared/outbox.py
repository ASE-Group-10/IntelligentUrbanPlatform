"""PostgreSQL outbox claims shared by the C05 and C06 publishers."""

from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg import sql

from app.shared.contracts import EventEnvelope


class OutboxUnavailableError(Exception):
    """The outbox could not be claimed or its publication marks committed."""


class OutboxBatch(Protocol):
    events: list[EventEnvelope]

    def mark_published(self, event_id: UUID) -> None: ...


class OutboxStore(Protocol):
    def outbox_batch(self, limit: int) -> AbstractContextManager[OutboxBatch]: ...


class PostgresOutboxBatch:
    """Hold the oldest available rows on the caller's transaction until confirmation."""

    def __init__(self, conn: psycopg.Connection, schema: str, limit: int) -> None:
        self._conn = conn
        self._table = sql.Identifier(schema, "outbox")
        rows = conn.execute(
            sql.SQL(
                "select envelope from {} where published_at is null "
                "order by seq limit %s for update skip locked"
            ).format(self._table),
            (limit,),
        ).fetchall()
        self.events = [EventEnvelope.model_validate(envelope) for (envelope,) in rows]

    def mark_published(self, event_id: UUID) -> None:
        self._conn.execute(
            sql.SQL("update {} set published_at = now() where event_id = %s").format(
                self._table
            ),
            (event_id,),
        )
