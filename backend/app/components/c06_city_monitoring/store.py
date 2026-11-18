"""City State persistence port for C06, plus an in-memory test double.

A transaction groups the state change, its history record and its outbox event:
either all of them are committed or none is (transactional outbox). Reads go
through ``read``, which sees committed state and never writes anything back.
"""

from bisect import bisect_right
from collections.abc import Iterator, Sequence
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

    def put_segment(self, condition: SegmentCondition, version: int) -> None:
        """Make ``condition`` current for its segment from City State ``version``.

        The store keeps every version so past snapshots can be rebuilt.
        """
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


class CityStateReader(Protocol):
    """Read-only view of committed City State for one scope."""

    def current_version(self) -> int:
        """Return the committed City State version (0 before the first change)."""
        ...

    def conditions(
        self, segment_keys: Sequence[str], at_version: int | None = None
    ) -> dict[str, SegmentCondition]:
        """Return the requested segments' conditions now, or as of ``at_version``.

        Segments without a condition are left out. The returned objects are copies:
        changing them never changes the store.
        """
        ...


class CityStateStore(Protocol):
    """Persistence for City State, history and outbox, partitioned by scope."""

    def read(self, state_scope: StateScope) -> AbstractContextManager[CityStateReader]:
        """Open a consistent, read-only view of committed state; never writes.

        Raises:
            StoreUnavailableError: if the store cannot be read.
        """
        ...

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
    segment_versions: dict[str, list[tuple[int, SegmentCondition]]] = field(
        default_factory=dict
    )
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

    def put_segment(self, condition: SegmentCondition, version: int) -> None:
        self._state.segments[condition.segment_key] = condition
        self._state.segment_versions.setdefault(condition.segment_key, []).append(
            (version, condition)
        )

    def set_version(self, version: int) -> None:
        self._state.version = version

    def record_observation(
        self, observation: Observation, receipt: IngestionReceipt
    ) -> None:
        self._state.history.append((observation, receipt))

    def enqueue(self, envelope: EventEnvelope) -> None:
        self._state.outbox.append(envelope)


class InMemoryReader:
    """In-memory ``CityStateReader`` over one committed ``ScopeState``."""

    def __init__(self, state: ScopeState) -> None:
        self._state = state

    def current_version(self) -> int:
        return self._state.version

    def conditions(
        self, segment_keys: Sequence[str], at_version: int | None = None
    ) -> dict[str, SegmentCondition]:
        found: dict[str, SegmentCondition] = {}
        for key in segment_keys:
            condition = self._condition(key, at_version)
            if condition is not None:
                found[key] = condition.model_copy(deep=True)
        return found

    def _condition(self, key: str, at_version: int | None) -> SegmentCondition | None:
        if at_version is None:
            return self._state.segments.get(key)
        versions = self._state.segment_versions.get(key, [])
        index = bisect_right(versions, at_version, key=lambda entry: entry[0])
        return versions[index - 1][1] if index else None


class InMemoryCityStateStore:
    """Test double for ``CityStateStore``; replaced by the Postgres store (#40).

    Each transaction works on a copy that replaces the stored state only when the
    ``with`` block exits normally, so failures leave nothing behind. Committed
    state is never changed in place, so a reader can use it without copying.
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

    @contextmanager
    def read(self, state_scope: StateScope) -> Iterator[InMemoryReader]:
        yield InMemoryReader(self._scopes.get(state_scope, ScopeState()))

    def seed_version(self, state_scope: StateScope, version: int) -> None:
        """Start a scope at ``version`` (tests use this to mirror the examples)."""
        self._scopes.setdefault(state_scope, ScopeState()).version = version

    def state(self, state_scope: StateScope) -> ScopeState:
        """Return a copy of everything held for ``state_scope``."""
        return deepcopy(self._scopes.get(state_scope, ScopeState()))
