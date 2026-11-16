"""UC05 ``ingestObservation``: turn a source observation into a City State change."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.components.c06_city_monitoring.contracts import (
    CityStateUpdated,
    IngestionReceipt,
    Observation,
    SegmentCondition,
)
from app.components.c06_city_monitoring.store import (
    CityStateStore,
    CityStateTransaction,
    StateScope,
    StoreUnavailableError,
)
from app.shared.contracts import Affected, EventEnvelope
from app.shared.result import Failure, Ok, Result

PRODUCER = "C06"
CITY_STATE_UPDATED_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceContext:
    """Where an observation comes from and which City State it may change.

    Source authentication is added once C02 provides an ``AuthContext``.
    """

    state_scope: StateScope
    correlation_id: UUID


class CityMonitoring:
    """C06 service API (UC05)."""

    def __init__(
        self,
        store: CityStateStore,
        clock: Callable[[], datetime],
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id

    def ingest_observation(
        self, context: SourceContext, observation: Observation
    ) -> Result[IngestionReceipt]:
        """Normalise, persist and merge one observation into City State.

        A newer observation becomes a new City State version, stored together
        with its history record and a ``CityStateUpdated`` outbox event. An
        observation older than the current value is kept in history only (AF4).
        Re-ingesting the same ``observationId`` returns the first receipt.

        Returns:
            ``Ok(IngestionReceipt)`` once everything is persisted;
            ``Failure("Invalid")`` for unusable values or unsupported targets;
            ``Failure("Unavailable")`` if the store fails (nothing is saved).
        """
        condition = _normalise(observation)
        if isinstance(condition, Failure):
            return condition
        try:
            with self._store.transaction(context.state_scope) as tx:
                return Ok(self._merge(tx, context, observation, condition))
        except StoreUnavailableError as error:
            return Failure("Unavailable", str(error))

    def _merge(
        self,
        tx: CityStateTransaction,
        context: SourceContext,
        observation: Observation,
        condition: SegmentCondition,
    ) -> IngestionReceipt:
        existing = tx.find_receipt(observation.observation_id)
        if existing is not None:
            return existing

        current = tx.get_segment(condition.segment_key)
        if current is not None and observation.observed_at < current.observed_at:
            receipt = IngestionReceipt(
                observation_id=observation.observation_id,
                disposition="history_only",
            )
            tx.record_observation(observation, receipt)
            return receipt

        previous = tx.current_version()
        version = previous + 1
        tx.put_segment(condition)
        tx.set_version(version)
        receipt = IngestionReceipt(
            observation_id=observation.observation_id,
            disposition="merged",
            snapshot_version=version,
        )
        tx.record_observation(observation, receipt)
        tx.enqueue(
            self._city_state_updated(context, observation, condition, previous, version)
        )
        return receipt

    def _city_state_updated(
        self,
        context: SourceContext,
        observation: Observation,
        condition: SegmentCondition,
        previous: int,
        version: int,
    ) -> EventEnvelope:
        payload = CityStateUpdated(
            snapshot_version=version,
            previous_version=previous,
            changes=[condition],
            degraded=False,
            stale_sources=[],
        )
        return EventEnvelope(
            event_id=self._new_id(),
            event_type="CityStateUpdated",
            schema_version=CITY_STATE_UPDATED_SCHEMA_VERSION,
            producer=PRODUCER,
            observed_at=observation.observed_at,
            emitted_at=self._clock(),
            correlation_id=context.correlation_id,
            affected=Affected(
                plan_ids=[], resource_ids=[], segment_keys=[condition.segment_key]
            ),
            entity_versions={"citySnapshot": version},
            scope=context.state_scope.scope,
            run_id=context.state_scope.run_id,
            payload=payload.model_dump(mode="json", by_alias=True),
        )


def _normalise(observation: Observation) -> SegmentCondition | Failure:
    """Map raw source values onto a ``SegmentCondition`` (contract units assumed)."""
    segment_key = observation.target.segment_key
    if segment_key is None:
        return Failure("Invalid", "only segmentKey targets are supported for now")
    values = observation.values
    try:
        return SegmentCondition(
            segment_key=segment_key,
            status=values.get("status"),
            speed_kmh=values.get("speedKmh"),
            delay_seconds=values.get("delaySeconds"),
            cause=values.get("cause"),
            valid_until=values.get("validUntil"),
            observed_at=observation.observed_at,
            quality="good",
            sources=[observation.source_id],
        )
    except ValidationError as error:
        return Failure("Invalid", f"observation values are not usable: {error}")
