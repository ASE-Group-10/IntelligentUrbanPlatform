"""Scoped C11 query orchestration; metric calculations belong to source adapters."""

from collections.abc import Callable
from datetime import datetime
from threading import RLock
from typing import Protocol

from pydantic import ValidationError

from app.components.c11_kpi_analytics.contracts import (
    AnalysisScope,
    Breakdown,
    KPIQuery,
    KPISet,
)
from app.shared.result import Failure, Ok, Result


class KPIReadSource(Protocol):
    """Read-only dependency: returns results for the complete requested scope.

    No formulas or producer payloads are implied by this private adapter boundary.
    Implementations return Unavailable for transient failures, without secrets.
    """

    def query(self, query: KPIQuery) -> Result[KPISet]: ...

    def breakdown(
        self, query: KPIQuery, metric_id: str, limit: int, cursor: str | None
    ) -> Result[Breakdown]: ...


class KPIAnalytics:
    """Authorise each read, validate the source, retain last successful values.

    The policy is supplied by the host's C02 adapter. There is deliberately no
    permissive default or invented AuthContext. The cache is process-local and
    bounded; restart/eviction loses stale fallback. It is not a persistent read
    model, and this class does not consume operational events.
    """

    def __init__(
        self,
        source: KPIReadSource,
        authorise: Callable[[object, AnalysisScope], bool],
        clock: Callable[[], datetime],
        max_cached_queries: int = 100,
    ) -> None:
        if type(max_cached_queries) is not int or max_cached_queries < 1:
            raise ValueError("cache capacity must be a positive integer")
        self._source = source
        self._authorise = authorise
        self._clock = clock
        self._capacity = max_cached_queries
        self._cache: dict[str, KPISet] = {}
        self._lock = RLock()

    def query_kpis(self, principal: object, query: KPIQuery) -> Result[KPISet]:
        """Refresh once; only transient failure may use an authorised cached set."""
        query = KPIQuery.model_validate(query.model_dump())
        if not self._authorise(principal, query.scope.model_copy(deep=True)):
            return Failure("Forbidden", "C11 scope is not authorised")
        # Serialise refreshes to prevent an older request replacing a newer cache.
        with self._lock:
            key = query.model_dump_json()
            result = self._source.query(query.model_copy(deep=True))
            if isinstance(result, Failure):
                if result.kind != "Unavailable":
                    return result
                previous = self._cache.get(key)
                if previous is None:
                    return Failure("Unavailable", "C11 source refresh unavailable")
                stale = previous.model_dump()
                stale.update(
                    status="stale",
                    failure="C11 source refresh unavailable",
                    generated_at=self._clock(),
                )
                return Ok(KPISet.model_validate(stale))
            try:
                current = KPISet.model_validate(result.value.model_dump())
            except (ValidationError, AttributeError):
                return Failure("Invalid", "C11 source returned an invalid KPI set")
            if not _matches(query, current) or current.status == "stale":
                return Failure("Invalid", "C11 source scope/version/status mismatch")
            current.generated_at = self._clock()
            current = KPISet.model_validate(current.model_dump())
            self._cache.pop(key, None)
            self._cache[key] = current.model_copy(deep=True)
            if len(self._cache) > self._capacity:
                del self._cache[next(iter(self._cache))]
            return Ok(current.model_copy(deep=True))

    def get_kpi_breakdown(
        self,
        principal: object,
        query: KPIQuery,
        metric_id: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Result[Breakdown]:
        """Read a bounded page without truncating its complete provenance."""
        query = KPIQuery.model_validate(query.model_dump())
        if not self._authorise(principal, query.scope.model_copy(deep=True)):
            return Failure("Forbidden", "C11 scope is not authorised")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or not isinstance(metric_id, str)
            or not 1 <= len(metric_id) <= 200
            or (
                cursor is not None
                and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 200)
            )
        ):
            return Failure("Invalid", "C11 invalid metric, page limit or cursor")
        result = self._source.breakdown(
            query.model_copy(deep=True), metric_id, limit, cursor
        )
        if isinstance(result, Failure):
            return result
        try:
            page = Breakdown.model_validate(result.value.model_dump())
        except (ValidationError, AttributeError):
            return Failure("Invalid", "C11 source returned an invalid breakdown")
        if not _matches(query, page) or page.metric_id != metric_id:
            return Failure("Invalid", "C11 breakdown scope/version/metric mismatch")
        if (
            len(page.records) > limit
            or len(page.source_event_ids) > 1000
            or len(set(page.source_event_ids)) != len(page.source_event_ids)
        ):
            return Failure("Invalid", "C11 page exceeds bounds; narrow the scope")
        return Ok(page.model_copy(deep=True))


def _matches(query: KPIQuery, result: KPIQuery) -> bool:
    return (
        query.scope == result.scope
        and query.metric_definition_version == result.metric_definition_version
    )
