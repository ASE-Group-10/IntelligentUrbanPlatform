"""Decision-record persistence port for C10, plus an in-memory test double.

``recordDecision`` and ``recordOutcome`` run inside the *caller's* database
transaction (internal.md): C10 writes on the connection it is given and never
commits or rolls back. Every write method therefore takes that connection as
``conn``. The in-memory double models it with ``InMemoryTransaction``: writes are
visible inside the transaction and reach the store only when the caller commits.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.components.c10_decision_records.contracts import AuditOutcome, DecisionRecord

type Connection = Any
"""The caller's open connection/transaction (a psycopg connection in production)."""


class StoreUnavailableError(Exception):
    """Decision storage cannot be reached; callers report Unavailable."""


@dataclass(frozen=True)
class StoredDecision:
    """A decision record exactly as received, plus what C10 adds on insert."""

    record: DecisionRecord
    request_hash: str  # detects an idempotency key reused with another body
    recorded_at: datetime


@dataclass(frozen=True)
class StoredOutcome:
    """One audit entry. Principal and correlation come from the AuthContext."""

    audit_id: UUID
    outcome: AuditOutcome
    principal_id: str
    correlation_id: UUID
    recorded_at: datetime


class DecisionStore(Protocol):
    """C10 storage. Every method may raise ``StoreUnavailableError``.

    Records are insert-only: there is no update or delete.
    """

    def find_by_idempotency_key(
        self, conn: Connection, idempotency_key: str
    ) -> StoredDecision | None:
        """The decision recorded under this key, including writes not yet
        committed on ``conn``."""
        ...

    def find_by_decision_id(
        self, conn: Connection, decision_id: UUID
    ) -> StoredDecision | None:
        """The decision with this ID, including writes not yet committed on
        ``conn``."""
        ...

    def insert_decision(self, conn: Connection, decision: StoredDecision) -> None:
        """Add the decision on ``conn``; it commits with the caller."""
        ...

    def insert_outcome(self, conn: Connection, outcome: StoredOutcome) -> None:
        """Add the audit entry on ``conn``; it commits with the caller."""
        ...


class TransactionClosedError(Exception):
    """A write or read used a transaction that was already committed or rolled
    back. Indicates a bug in the caller, not an expected outcome."""


@dataclass
class InMemoryTransaction:
    """Stands in for the caller's connection. Its writes are visible to reads on
    the same transaction and reach the store only on ``commit()``."""

    store: "InMemoryDecisionStore"
    decisions: dict[UUID, StoredDecision] = field(default_factory=dict)
    outcomes: list[StoredOutcome] = field(default_factory=list)
    closed: bool = False

    def commit(self) -> None:
        self._check_open()
        self.store.decisions.update(self.decisions)
        self.store.outcomes.extend(self.outcomes)
        self.closed = True

    def rollback(self) -> None:
        self._check_open()
        self.closed = True

    def _check_open(self) -> None:
        if self.closed:
            raise TransactionClosedError("transaction already finished")


@dataclass
class InMemoryDecisionStore:
    """Test double. ``decisions`` and ``outcomes`` hold committed data only.

    Set ``available = False`` to simulate a storage outage.
    """

    decisions: dict[UUID, StoredDecision] = field(default_factory=dict)
    outcomes: list[StoredOutcome] = field(default_factory=list)
    available: bool = True

    def begin(self) -> InMemoryTransaction:
        """Open a transaction, as the calling component would on its connection."""
        return InMemoryTransaction(self)

    def _visible(self, conn: InMemoryTransaction) -> dict[UUID, StoredDecision]:
        if not self.available:
            raise StoreUnavailableError("decision store is unavailable")
        conn._check_open()
        return {**self.decisions, **conn.decisions}

    def find_by_idempotency_key(
        self, conn: InMemoryTransaction, idempotency_key: str
    ) -> StoredDecision | None:
        for stored in self._visible(conn).values():
            if stored.record.idempotency_key == idempotency_key:
                return stored
        return None

    def find_by_decision_id(
        self, conn: InMemoryTransaction, decision_id: UUID
    ) -> StoredDecision | None:
        return self._visible(conn).get(decision_id)

    def insert_decision(
        self, conn: InMemoryTransaction, decision: StoredDecision
    ) -> None:
        self._visible(conn)  # availability and open-transaction checks
        conn.decisions[decision.record.decision_id] = decision

    def insert_outcome(self, conn: InMemoryTransaction, outcome: StoredOutcome) -> None:
        self._visible(conn)
        conn.outcomes.append(outcome)
