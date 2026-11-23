"""Demo feed: inject the scenario's road closure into C06 (#42, #72 hop 7).

This is the simulated input that starts the walking skeleton's event chain:
C06 stores the closure and queues ``CityStateUpdated``; the C06 publisher sends
it to RabbitMQ and the C07 worker picks it up.

Run from ``backend/`` with the database migrated::

    DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop \\
        uv run python -m app.demo.feed            # the scenario's closure
    ... uv run python -m app.demo.feed --fresh    # a new closure, new IDs, now

Feeding the scenario's closure again changes nothing (same observation ID).
"""

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import UUID4

from app.components.c06_city_monitoring.contracts import (
    CitySnapshotQuery,
    IngestionDisposition,
    Observation,
    ObservationTarget,
    SegmentCause,
    SegmentStatus,
    SnapshotScope,
)
from app.components.c06_city_monitoring.ingestion import CityMonitoring, SourceContext
from app.components.c06_city_monitoring.store import CityStateStore, StateScope
from app.shared.contracts import ContractModel, SegmentKey, UtcDatetime
from app.shared.result import Failure, Ok, Result

SCENARIO_PATH = (
    Path(__file__).resolve().parents[3] / "fixtures" / "dublin-scenario.json"
)


class ScenarioClosure(ContractModel):
    observation_id: UUID4
    source_id: str
    segment_key: SegmentKey
    cause: SegmentCause
    observed_at: UtcDatetime
    valid_until: UtcDatetime | None = None


class Scenario(ContractModel):
    """Shared demo data (``fixtures/dublin-scenario.json``)."""

    scenario_id: str
    run_id: UUID4
    seed: int
    start_time: UtcDatetime
    correlation_id: UUID4
    closure: ScenarioClosure
    segments: list[dict[str, Any]]


class FeedReport(ContractModel):
    run_id: UUID4
    correlation_id: UUID4
    observation_id: UUID4
    segment_key: SegmentKey
    disposition: IngestionDisposition
    already_ingested: bool
    snapshot_version: int | None
    queued_event_id: UUID4 | None
    segment_status: SegmentStatus | None


def load_scenario(path: Path = SCENARIO_PATH) -> Scenario:
    return Scenario.model_validate_json(path.read_text(encoding="utf-8"))


def feed_closure(
    store: CityStateStore,
    scenario: Scenario,
    clock: Callable[[], datetime],
    fresh: bool = False,
) -> Result[FeedReport]:
    """Ingest the scenario's closure (or, with ``fresh``, a new one) into C06."""
    closure = scenario.closure
    if fresh:
        observation_id, observed_at, correlation_id = uuid4(), clock(), uuid4()
        values: dict[str, Any] = {"status": "closed", "cause": closure.cause}
    else:
        observation_id = closure.observation_id
        observed_at = closure.observed_at
        correlation_id = scenario.correlation_id
        values = {"status": "closed", "cause": closure.cause}
        if closure.valid_until:
            values["validUntil"] = closure.valid_until.isoformat().replace(
                "+00:00", "Z"
            )
    observation = Observation(
        observation_id=observation_id,
        source_id=closure.source_id,
        kind="closure",
        target=ObservationTarget(segment_key=closure.segment_key),
        values=values,
        observed_at=observed_at,
    )

    queued: list[UUID] = []

    def new_event_id() -> UUID:
        queued.append(uuid4())
        return queued[-1]

    service = CityMonitoring(store=store, clock=clock, new_id=new_event_id)
    scope = StateScope(scope="simulation", run_id=scenario.run_id)
    ingested = service.ingest_observation(
        SourceContext(state_scope=scope, correlation_id=correlation_id), observation
    )
    if isinstance(ingested, Failure):
        return ingested
    receipt = ingested.value

    snapshot = service.get_city_snapshot(
        scope,
        CitySnapshotQuery(scope=SnapshotScope(segment_keys=[closure.segment_key])),
    )
    status = None
    if isinstance(snapshot, Ok) and snapshot.value.segments:
        status = snapshot.value.segments[0].status

    return Ok(
        FeedReport(
            run_id=scenario.run_id,
            correlation_id=correlation_id,
            observation_id=observation_id,
            segment_key=closure.segment_key,
            disposition=receipt.disposition,
            already_ingested=not queued and receipt.disposition == "merged",
            snapshot_version=receipt.snapshot_version,
            queued_event_id=queued[0] if queued else None,
            segment_status=status,
        )
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    from app.components.c06_city_monitoring.postgres_store import (
        PostgresCityStateStore,
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", type=Path, default=SCENARIO_PATH)
    parser.add_argument(
        "--fresh", action="store_true", help="inject a new closure with new IDs, now"
    )
    args = parser.parse_args(argv)

    scenario = load_scenario(args.scenario)
    store = PostgresCityStateStore(os.environ["DATABASE_URL"])
    result = feed_closure(store, scenario, lambda: datetime.now(UTC), args.fresh)
    if isinstance(result, Failure):
        print(f"C06 refused the closure: {result.kind}: {result.message}")
        return 1

    report = result.value
    print(f"run:            {report.run_id}")
    print(f"correlationId:  {report.correlation_id}")
    print(f"closure:        {report.segment_key} (observation {report.observation_id})")
    if report.already_ingested:
        print("result:         already ingested earlier; nothing changed")
    else:
        version = report.snapshot_version
        print(f"result:         {report.disposition}, City State v{version}")
        print(f"queued event:   CityStateUpdated {report.queued_event_id}")
    print(f"segment now:    {report.segment_status}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
