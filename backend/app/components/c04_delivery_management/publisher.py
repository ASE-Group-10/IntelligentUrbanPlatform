"""Publish committed C04 delivery event envelopes to their durable subscriber queues."""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql

from app.shared.outbox import OutboxUnavailableError, PostgresOutboxBatch
from app.shared.publisher import serve

# A locked earlier delivery_version is still pending for every other publisher
# until its broker confirmation is committed. Other scoped deliveries remain
# claimable in parallel.
_NEXT_DELIVERY_VERSION = sql.SQL("""
    not exists (
        select 1 from c04_delivery_management.outbox as earlier
         where earlier.scope_key = pending.scope_key
           and earlier.delivery_id = pending.delivery_id
           and earlier.delivery_version < pending.delivery_version
           and earlier.published_at is null
    )
""")


class PostgresDeliveryOutbox:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def outbox_batch(self, limit: int) -> Iterator[PostgresOutboxBatch]:
        try:
            with psycopg.connect(self._conninfo, connect_timeout=5) as conn:
                yield PostgresOutboxBatch(
                    conn,
                    "c04_delivery_management",
                    limit,
                    eligible=_NEXT_DELIVERY_VERSION,
                )
        except psycopg.Error as error:
            raise OutboxUnavailableError(
                "C04 delivery outbox is unavailable"
            ) from error


if __name__ == "__main__":  # pragma: no cover - worker
    serve("C04", PostgresDeliveryOutbox(os.environ["DATABASE_URL"]))
