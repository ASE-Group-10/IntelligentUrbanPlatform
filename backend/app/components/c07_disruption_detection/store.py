"""Disruption persistence port for C07, plus an in-memory test double.

A transaction groups the processed-event mark and the saved disruption: either
both are committed or neither is. Live and every simulation run are kept apart.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.components.c07_disruption_detection.contracts import Disruption
from app.shared.contracts import RunScope


class DisruptionStoreUnavailableError(Exception):
    """The disruption store could not complete the operation; nothing was saved."""


class DisruptionTransaction(Protocol):
    """Reads and writes inside one atomic disruption transaction."""

    def is_processed(self, event_id: UUID) -> bool:
        """Return whether this event was already evaluated in this scope."""
        ...

    def mark_processed(self, event_id: UUID) -> None:
        """Record that ``event_id`` was evaluated, so a redelivery is a duplicate."""
        ...

    def save_disruption(self, disruption: Disruption) -> None:
        """Insert the disruption, or replace the one with the same ``disruption_id``."""
        ...

    def disruption_for_event(self, event_id: UUID) -> Disruption | None:
        """Return the disruption ``event_id`` triggered in this scope, if any."""
        ...


class DisruptionStore(Protocol):
    """Persistence for disruptions and processed events, partitioned by scope."""

    def transaction(
        self, scope: RunScope, run_id: UUID | None
    ) -> AbstractContextManager[DisruptionTransaction]:
        """Open a transaction that commits on normal exit and rolls back on error.

        ``live`` and each simulation ``run_id`` are separate partitions.

        Raises:
            DisruptionStoreUnavailableError: if the transaction cannot be opened
                or committed.
        """
        ...


@dataclass
class ScopeState:
    """Everything the in-memory store holds for one scope."""

    processed: set[UUID] = field(default_factory=set)
    disruptions: dict[UUID, Disruption] = field(default_factory=dict)


class InMemoryDisruptionTransaction:
    """In-memory ``DisruptionTransaction`` working on a private copy of the state."""

    def __init__(self, state: ScopeState) -> None:
        self._state = state

    def is_processed(self, event_id: UUID) -> bool:
        return event_id in self._state.processed

    def mark_processed(self, event_id: UUID) -> None:
        self._state.processed.add(event_id)

    def save_disruption(self, disruption: Disruption) -> None:
        self._state.disruptions[disruption.disruption_id] = disruption

    def disruption_for_event(self, event_id: UUID) -> Disruption | None:
        for disruption in self._state.disruptions.values():
            if event_id in disruption.trigger_event_ids:
                return deepcopy(disruption)
        return None


class InMemoryDisruptionStore:
    """Test double for ``DisruptionStore``; replaced by the Postgres store later.

    Each transaction works on a copy that replaces the stored state only when the
    ``with`` block exits normally, so failures leave nothing behind.
    """

    def __init__(
        self,
        transaction_type: type[
            InMemoryDisruptionTransaction
        ] = InMemoryDisruptionTransaction,
    ) -> None:
        self._transaction_type = transaction_type
        self._scopes: dict[tuple[RunScope, UUID | None], ScopeState] = {}

    @contextmanager
    def transaction(
        self, scope: RunScope, run_id: UUID | None
    ) -> Iterator[InMemoryDisruptionTransaction]:
        working = deepcopy(self._scopes.get((scope, run_id), ScopeState()))
        yield self._transaction_type(working)
        self._scopes[(scope, run_id)] = working

    def disruptions(self, scope: RunScope, run_id: UUID | None) -> list[Disruption]:
        """Return copies of the disruptions saved in one scope, oldest first."""
        state = self._scopes.get((scope, run_id), ScopeState())
        return deepcopy(list(state.disruptions.values()))

    def processed(self, scope: RunScope, run_id: UUID | None) -> set[UUID]:
        """Return the ids of the events marked as processed in one scope."""
        return set(self._scopes.get((scope, run_id), ScopeState()).processed)
