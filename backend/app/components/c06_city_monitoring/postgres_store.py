"""PostgreSQL implementation of ``CityStateStore`` (schema ``c06_city_monitoring``).

Tables come from ``database/migrations/20261119180000_create_c06_city_state_tables``.

Writes run in one database transaction that first locks the scope's
``city_state`` row, so concurrent ingestions into the same scope are serialised
and never produce the same version twice. The state change, its history record
and its outbox event commit together or not at all (transactional outbox).
Reads run in a read-only, repeatable-read transaction and never write.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg import IsolationLevel
from psycopg.types.json import Jsonb

from app.components.c06_city_monitoring.contracts import (
    IngestionReceipt,
    Observation,
    SegmentCondition,
)
from app.components.c06_city_monitoring.store import (
    ScopeState,
    StateScope,
    StoreUnavailableError,
)
from app.shared.contracts import ContractModel, EventEnvelope
from app.shared.outbox import PostgresOutboxBatch


def scope_key(state_scope: StateScope) -> str:
    """Key of a scope's ``city_state`` row: ``live`` or ``simulation:<runId>``."""
    if state_scope.scope == "live":
        return "live"
    return f"simulation:{state_scope.run_id}"


def _json(model: ContractModel) -> Jsonb:
    return Jsonb(model.model_dump(mode="json", by_alias=True))


class PostgresTransaction:
    """``CityStateTransaction`` inside one open database transaction."""

    def __init__(self, conn: psycopg.Connection, key: str) -> None:
        self._conn = conn
        self._key = key

    def find_receipt(self, observation_id: UUID) -> IngestionReceipt | None:
        row = self._conn.execute(
            "select receipt from c06_city_monitoring.observations"
            " where scope_key = %s and observation_id = %s",
            (self._key, observation_id),
        ).fetchone()
        return IngestionReceipt.model_validate(row[0]) if row else None

    def current_version(self) -> int:
        row = self._conn.execute(
            "select version from c06_city_monitoring.city_state where scope_key = %s",
            (self._key,),
        ).fetchone()
        return row[0] if row else 0

    def get_segment(self, segment_key: str) -> SegmentCondition | None:
        row = self._conn.execute(
            "select condition from c06_city_monitoring.segment_conditions"
            " where scope_key = %s and segment_key = %s"
            " order by version desc limit 1",
            (self._key, segment_key),
        ).fetchone()
        return SegmentCondition.model_validate(row[0]) if row else None

    def put_segment(self, condition: SegmentCondition, version: int) -> None:
        self._conn.execute(
            "insert into c06_city_monitoring.segment_conditions"
            " (scope_key, segment_key, version, condition) values (%s, %s, %s, %s)",
            (self._key, condition.segment_key, version, _json(condition)),
        )

    def set_version(self, version: int) -> None:
        self._conn.execute(
            "update c06_city_monitoring.city_state"
            " set version = %s, updated_at = now() where scope_key = %s",
            (version, self._key),
        )

    def record_observation(
        self, observation: Observation, receipt: IngestionReceipt
    ) -> None:
        self._conn.execute(
            "insert into c06_city_monitoring.observations"
            " (scope_key, observation_id, observation, receipt)"
            " values (%s, %s, %s, %s)",
            (self._key, observation.observation_id, _json(observation), _json(receipt)),
        )

    def enqueue(self, envelope: EventEnvelope) -> None:
        self._conn.execute(
            "insert into c06_city_monitoring.outbox"
            " (event_id, scope_key, event_type, envelope) values (%s, %s, %s, %s)",
            (envelope.event_id, self._key, envelope.event_type, _json(envelope)),
        )


class PostgresReader:
    """``CityStateReader`` inside a read-only, repeatable-read transaction."""

    def __init__(self, conn: psycopg.Connection, key: str) -> None:
        self._conn = conn
        self._key = key

    def current_version(self) -> int:
        row = self._conn.execute(
            "select version from c06_city_monitoring.city_state where scope_key = %s",
            (self._key,),
        ).fetchone()
        return row[0] if row else 0

    def conditions(
        self, segment_keys: Sequence[str], at_version: int | None = None
    ) -> dict[str, SegmentCondition]:
        rows = self._conn.execute(
            "select distinct on (segment_key) segment_key, condition"
            " from c06_city_monitoring.segment_conditions"
            " where scope_key = %s and segment_key = any(%s)"
            " and (%s::integer is null or version <= %s::integer)"
            " order by segment_key, version desc",
            (self._key, list(segment_keys), at_version, at_version),
        ).fetchall()
        return {key: SegmentCondition.model_validate(value) for key, value in rows}


class PostgresCityStateStore:
    """``CityStateStore`` backed by PostgreSQL (local Docker or Supabase).

    Args:
        conninfo: libpq connection string, e.g. the ``DATABASE_URL`` from ``.env``.
    """

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self, state_scope: StateScope) -> Iterator[PostgresTransaction]:
        key = scope_key(state_scope)
        with self._connect() as conn:
            try:
                with conn.transaction():
                    self._lock_scope(conn, state_scope, key)
                    yield PostgresTransaction(conn, key)
            except psycopg.OperationalError as error:
                raise StoreUnavailableError(
                    f"City State store failed: {error}"
                ) from error

    @contextmanager
    def read(self, state_scope: StateScope) -> Iterator[PostgresReader]:
        with self._connect() as conn:
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            try:
                with conn.transaction():
                    yield PostgresReader(conn, scope_key(state_scope))
            except psycopg.OperationalError as error:
                raise StoreUnavailableError(
                    f"City State store failed: {error}"
                ) from error

    @contextmanager
    def outbox_batch(self, limit: int) -> Iterator["PostgresOutboxBatch"]:
        with self._connect() as conn:
            try:
                with conn.transaction():
                    yield PostgresOutboxBatch(conn, "c06_city_monitoring", limit)
            except psycopg.OperationalError as error:
                raise StoreUnavailableError(
                    f"City State store failed: {error}"
                ) from error

    def seed_version(self, state_scope: StateScope, version: int) -> None:
        """Start a scope at ``version`` (tests use this to mirror the examples)."""
        with self.transaction(state_scope) as tx:
            tx.set_version(version)

    def state(self, state_scope: StateScope) -> ScopeState:
        """Return everything held for ``state_scope`` (for tests and diagnostics)."""
        key = scope_key(state_scope)
        with self._connect() as conn:
            version_row = conn.execute(
                "select version from c06_city_monitoring.city_state"
                " where scope_key = %s",
                (key,),
            ).fetchone()
            condition_rows = conn.execute(
                "select segment_key, version, condition"
                " from c06_city_monitoring.segment_conditions"
                " where scope_key = %s order by segment_key, version",
                (key,),
            ).fetchall()
            history_rows = conn.execute(
                "select observation, receipt from c06_city_monitoring.observations"
                " where scope_key = %s order by seq",
                (key,),
            ).fetchall()
            outbox_rows = conn.execute(
                "select envelope, published_at is not null"
                " from c06_city_monitoring.outbox where scope_key = %s order by seq",
                (key,),
            ).fetchall()

        state = ScopeState(version=version_row[0] if version_row else 0)
        for segment_key, version, value in condition_rows:
            condition = SegmentCondition.model_validate(value)
            state.segment_versions.setdefault(segment_key, []).append(
                (version, condition)
            )
            state.segments[segment_key] = condition
        state.history = [
            (Observation.model_validate(o), IngestionReceipt.model_validate(r))
            for o, r in history_rows
        ]
        state.outbox = [EventEnvelope.model_validate(e) for e, _ in outbox_rows]
        state.published = {
            envelope.event_id
            for envelope, (_, published) in zip(state.outbox, outbox_rows, strict=True)
            if published
        }
        return state

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._conninfo)
        except psycopg.OperationalError as error:
            raise StoreUnavailableError(
                f"City State store unreachable: {error}"
            ) from error

    @staticmethod
    def _lock_scope(
        conn: psycopg.Connection, state_scope: StateScope, key: str
    ) -> None:
        conn.execute(
            "insert into c06_city_monitoring.city_state (scope_key, scope, run_id)"
            " values (%s, %s, %s) on conflict (scope_key) do nothing",
            (key, state_scope.scope, state_scope.run_id),
        )
        conn.execute(
            "select version from c06_city_monitoring.city_state"
            " where scope_key = %s for update",
            (key,),
        )
