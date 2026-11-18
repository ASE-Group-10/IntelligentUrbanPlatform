"""UC05 ``getCitySnapshot``: read a versioned City State with quality labels."""

from collections.abc import Iterable
from datetime import datetime, timedelta

from app.components.c06_city_monitoring.contracts import (
    CitySnapshot,
    CitySnapshotQuery,
    SegmentCondition,
)
from app.components.c06_city_monitoring.store import (
    CityStateStore,
    StateScope,
    StoreUnavailableError,
)
from app.shared.contracts import Quality
from app.shared.result import Failure, Ok, Result

DEFAULT_MAX_AGE_SECONDS = 900
"""Agreed default: values older than 15 minutes are reported as ``degraded``."""

_QUALITY_RANK: dict[Quality, int] = {"good": 0, "degraded": 1, "unknown": 2}


def get_city_snapshot(
    store: CityStateStore,
    now: datetime,
    state_scope: StateScope,
    query: CitySnapshotQuery,
) -> Result[CitySnapshot]:
    """Return the requested segments' conditions at the current or a past version.

    Segments without a recorded condition are omitted (absent means open, good).
    For the current version, a ``good`` value older than the freshness limit is
    returned as ``degraded`` and its sources are listed as stale. Past versions
    (``atVersion``) are returned as recorded.

    Returns:
        ``Ok(CitySnapshot)``;
        ``Failure("Invalid")`` for area (bbox) queries, which need segment geometry;
        ``Failure("NotFound")`` if ``atVersion`` is newer than the current version;
        ``Failure("Unavailable")`` if no City State exists yet or the store fails.
    """
    keys = query.scope.segment_keys
    if keys is None:
        return Failure(
            "Invalid",
            "area (bbox) queries need segment geometry, which C06 does not hold yet; "
            "query by segmentKeys",
        )

    try:
        with store.transaction(state_scope) as tx:
            current = tx.current_version()
            if current == 0:
                return Failure(
                    "Unavailable", "no City State recorded for this scope yet"
                )
            if query.at_version is not None and query.at_version > current:
                return Failure(
                    "NotFound",
                    f"City State version {query.at_version} does not exist "
                    f"(current is {current})",
                )
            conditions = tx.segments(at_version=query.at_version)
    except StoreUnavailableError as error:
        return Failure("Unavailable", str(error))

    selected = [conditions[key] for key in dict.fromkeys(keys) if key in conditions]
    stale_sources: set[str] = set()
    if query.at_version is None:
        max_age = _max_age(query)
        selected = [_with_freshness(c, now, max_age, stale_sources) for c in selected]

    return Ok(
        CitySnapshot(
            snapshot_version=query.at_version or current,
            generated_at=now,
            segments=selected,
            stale_sources=sorted(stale_sources),
            overall_quality=_worst(c.quality for c in selected),
        )
    )


def _max_age(query: CitySnapshotQuery) -> timedelta:
    policy = query.freshness_policy
    seconds = policy.max_age_seconds if policy and policy.max_age_seconds else None
    return timedelta(seconds=seconds or DEFAULT_MAX_AGE_SECONDS)


def _with_freshness(
    condition: SegmentCondition,
    now: datetime,
    max_age: timedelta,
    stale_sources: set[str],
) -> SegmentCondition:
    if condition.quality != "good" or now - condition.observed_at <= max_age:
        return condition
    stale_sources.update(condition.sources)
    return condition.model_copy(update={"quality": "degraded"})


def _worst(qualities: Iterable[Quality]) -> Quality:
    return max(qualities, key=_QUALITY_RANK.__getitem__, default="good")
