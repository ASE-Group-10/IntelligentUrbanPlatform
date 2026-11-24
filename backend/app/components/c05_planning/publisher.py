"""Publish committed C05 PlanReady envelopes to their durable subscriber queues."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from app.shared.outbox import OutboxUnavailableError, PostgresOutboxBatch
from app.shared.publisher import serve


class PostgresPlanOutbox:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def outbox_batch(self, limit: int) -> Iterator[PostgresOutboxBatch]:
        try:
            with psycopg.connect(self._conninfo, connect_timeout=5) as conn:
                yield PostgresOutboxBatch(conn, "c05_planning", limit)
        except psycopg.Error as error:
            raise OutboxUnavailableError("C05 plan outbox is unavailable") from error


if __name__ == "__main__":  # pragma: no cover - worker
    serve("C05", PostgresPlanOutbox(os.environ["DATABASE_URL"]))
