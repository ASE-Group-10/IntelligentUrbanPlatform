"""City State persistence port for C06, plus an in-memory test double.

A transaction groups the state change, its history record and its outbox event:
either all of them are committed or none is (transactional outbox).
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.components.c06_city_monitoring.contracts import (
    IngestionReceipt,
    Observation,
    SegmentCondition,
)
from app.shared.contracts import EventEnvelope, RunScope


@dataclass(frozen=True)
class StateScope:
    """Which City State to read or write: ``live`` or one simulation run.

    Raises:
        ValueError: if ``simulation`` has no ``run_id`` or ``live`` has one.
    """

    scope: RunScope
    run_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.scope == "simulation" and self.run_id is None:
            raise ValueError("simulation scope requires a run_id")
        if self.scope == "live" and self.run_id is not None:
            raise ValueError("live scope must not have a run_id")


class StoreUnavailableError(Exception):
    """The City State store could not complete the operation; nothing was saved."""


class CityStateTransaction(Protocol):
    """Reads and writes inside one atomic City State transaction."""

    def find_receipt(self, observation_id: UUID) -> IngestionReceipt | None:
        """Return the receipt of an already-ingested observation, if any."""
        ...

    def current_version(self) -> int:
        """Return the current City State version (0 before the first change)."""
        ...

    def get_segment(self, segment_key: str) -> SegmentCondition | None:
        """Return the current condition of a segment, if one is held."""
        ...

    def put_segment(self, condition: SegmentCondition) -> None:
        """Replace the current condition of ``condition.segment_key``."""
        ...

    def set_version(self, version: int) -> None:
        """Record the new City State version."""
        ...

    def record_observation(
        self, observation: Observation, receipt: IngestionReceipt
    ) -> None:
        """Append the raw observation and its receipt to history."""
        ...

    def enqueue(self, envelope: EventEnvelope) -> None:
        """Add an event to the outbox for later publication."""
        ...


class CityStateStore(Protocol):
    """Persistence for City State, history and outbox, partitioned by scope."""

    def transaction(
        self, state_scope: StateScope
    ) -> AbstractContextManager[CityStateTransaction]:
        """Open a transaction that commits on normal exit and rolls back on error.

        Raises:
            StoreUnavailableError: if the transaction cannot be opened or committed.
        """
        ...


@dataclass
class ScopeState:
    """Everything the in-memory store holds for one scope."""

    version: int = 0
    segments: dict[str, SegmentCondition] = field(default_factory=dict)
    history: list[tuple[Observation, IngestionReceipt]] = field(default_factory=list)
    outbox: list[EventEnvelope] = field(default_factory=list)


class InMemoryTransaction:
    """In-memory ``CityStateTransaction`` working on a private copy of the state."""

    def __init__(self, state: ScopeState) -> None:
        self._state = state

    def find_receipt(self, observation_id: UUID) -> IngestionReceipt | None:
        for observation, receipt in self._state.history:
            if observation.observation_id == observation_id:
                return receipt
        return None

    def current_version(self) -> int:
        return self._state.version

    def get_segment(self, segment_key: str) -> SegmentCondition | None:
        return self._state.segments.get(segment_key)

    def put_segment(self, condition: SegmentCondition) -> None:
        self._state.segments[condition.segment_key] = condition

    def set_version(self, version: int) -> None:
        self._state.version = version

    def record_observation(
        self, observation: Observation, receipt: IngestionReceipt
    ) -> None:
        self._state.history.append((observation, receipt))

    def enqueue(self, envelope: EventEnvelope) -> None:
        self._state.outbox.append(envelope)


class InMemoryCityStateStore:
    """Test double for ``CityStateStore``; replaced by the Postgres store (#5).

    Each transaction works on a copy that replaces the stored state only when the
    ``with`` block exits normally, so failures leave nothing behind.
    """

    def __init__(
        self, transaction_type: type[InMemoryTransaction] = InMemoryTransaction
    ) -> None:
        self._transaction_type = transaction_type
        self._scopes: dict[StateScope, ScopeState] = {}

    @contextmanager
    def transaction(self, state_scope: StateScope) -> Iterator[InMemoryTransaction]:
        working = deepcopy(self._scopes.get(state_scope, ScopeState()))
        yield self._transaction_type(working)
        self._scopes[state_scope] = working

    def seed_version(self, state_scope: StateScope, version: int) -> None:
        """Start a scope at ``version`` (tests use this to mirror the examples)."""
        self._scopes.setdefault(state_scope, ScopeState()).version = version

    def state(self, state_scope: StateScope) -> ScopeState:
        """Return a copy of everything held for ``state_scope``."""
        return deepcopy(self._scopes.get(state_scope, ScopeState()))
